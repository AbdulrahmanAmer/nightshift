"""Format presets as data (spec 17.1): `presets/formats/<id>.yaml` loaded into `ns_common.models.Format`.

Adding a format never touches Python: drop a YAML file next to the shipped ones. `validate_all` checks every
file (model, id = file name, regions inside the canvas, caption styles that exist); `formats_allowed` and
`ensure_allowed` apply a channel's list of permitted formats.

Loading is safe: one broken file is listed by `load_problems` and left out, it never stops the other formats,
and a file saved while a process runs is seen at its next lookup (see `_load_dir`). Renders draw from the
format their job recorded (`ns_studio.media.job_formats`); the helpers below say how a written value reaches
a render (`written`, `fps_of`, `shot_range`, ...) while a value a file does not write keeps today's behaviour.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ValidationError

from ns_common.atomic import read_text_retry
from ns_common.models import Channel, Format
from ns_common.paths import Paths
from ns_studio.errors import StudioError
from ns_studio.media.presets import PRESETS_DIR, load_caption_styles

FORMATS_DIR = PRESETS_DIR / "formats"
# Kinds a series design may pick (clip layouts are chosen per clip by the clipper).
DESIGN_KINDS = ("short", "long", "square")
STORY_KINDS = ("short", "long", "square")

Variant = Literal["vertical", "landscape"]
Orientation = Literal["portrait", "landscape"]
Signature = tuple[tuple[str, int, int], ...]


def fields_problem(e: ValidationError) -> str:
    """The model's complaints as plain text that names each field (`canvas.w: Input should be ...`)."""
    parts: list[str] = []
    for err in e.errors():
        where = ".".join(str(p) for p in err["loc"]) or "format"
        if err["type"] == "missing":
            parts.append(f"{where} is required")
        elif err["type"] == "extra_forbidden":
            parts.append(f"{where} is not a known setting")
        else:
            parts.append(f"{where}: {err['msg']}")
    return "; ".join(parts)[:400]


def _parse_file(path: Path) -> tuple[dict[str, Any], Format]:
    """(the mapping as written, the validated model) of one file; NS-E320 naming the file and the field."""
    try:
        raw = yaml.safe_load(read_text_retry(path))
    except (OSError, yaml.YAMLError) as e:
        raise StudioError("NS-E320", f"formats/{path.name}: {str(e)[:300]}") from e
    if not isinstance(raw, dict):
        raise StudioError("NS-E320", f"formats/{path.name}: a format must be a mapping of settings")
    try:
        fmt = Format.model_validate(raw)
    except ValidationError as e:
        raise StudioError("NS-E320", f"formats/{path.name}: {fields_problem(e)}") from e
    if fmt.id != path.stem:
        raise StudioError("NS-E320", f"formats/{path.name}: id {fmt.id!r} must equal the file name")
    return raw, fmt


def _load_file(path: Path) -> Format:
    return _parse_file(path)[1]


@dataclass(frozen=True)
class FolderRead:
    """What one formats folder held when it was read: good formats, their files as written, problems."""

    formats: dict[str, Format]
    raw: dict[str, dict[str, Any]]
    problems: dict[str, str]


def _signature(d: Path) -> Signature:
    """Name, modification time and size of every `*.yaml` in `d`: changes when a file is saved or removed."""
    out: list[tuple[str, int, int]] = []
    try:
        for p in sorted(d.glob("*.yaml")):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append((p.name, st.st_mtime_ns, st.st_size))
    except OSError:
        return ()
    return tuple(out)


_FOLDERS: dict[str, tuple[Signature, FolderRead]] = {}


def _read_folder(d: Path) -> FolderRead:
    formats: dict[str, Format] = {}
    raw: dict[str, dict[str, Any]] = {}
    problems: dict[str, str] = {}
    for p in sorted(d.glob("*.yaml")) if d.is_dir() else []:
        try:
            raw[p.stem], formats[p.stem] = _parse_file(p)
        except StudioError as e:
            raw.pop(p.stem, None)
            problems[p.stem] = e.detail
    return FolderRead(formats, raw, problems)


def _load_dir(directory: str) -> FolderRead:
    """The folder's formats, re-read whenever a file in it was added, saved or removed.

    The result is cached per folder and checked against a signature of its files on every call, so a process
    that outlives an edit (the dashboard, which also runs the series Workshop in-process) sees saved files at
    once, without `forget_loaded_formats`. A file that does not load is listed in `problems` and left out.
    """
    d = Path(directory)
    sig = _signature(d)
    hit = _FOLDERS.get(directory)
    if hit is not None and hit[0] == sig:
        return hit[1]
    read = _read_folder(d)
    _FOLDERS[directory] = (sig, read)
    return read


def forget_loaded_formats() -> None:
    """Drop every folder read so far (saved files are noticed anyway; this forces a re-read)."""
    _FOLDERS.clear()


def _folders(directory: Path | None, paths: Paths | None) -> list[FolderRead]:
    """The folders a lookup reads: `directory` alone when given (tests), else shipped then the owner's."""
    if directory is not None:
        return [_load_dir(str(directory))]
    return [_load_dir(str(FORMATS_DIR)), _load_dir(str((paths or Paths.from_env()).formats_dir))]


def load_formats(directory: Path | None = None, *, paths: Paths | None = None) -> dict[str, Format]:
    """Every shipped preset keyed by id, overlaid with the owner's edited or new formats.

    The dashboard's Library > Formats writes `<home>\\config\\formats\\<id>.yaml` (`ns_dashboard.formats`);
    a file with a shipped id shadows that preset, any other id is a new format. `directory` replaces the
    shipped folder and skips the overlay (tests). A file that does not load is left out (`load_problems`
    says why): one broken duplicate no longer stops every other format.
    """
    out: dict[str, Format] = {}
    for read in _folders(directory, paths):
        out.update(read.formats)
    return out


def load_problems(directory: Path | None = None, *, paths: Paths | None = None) -> dict[str, str]:
    """{format id (file name): what is wrong} for every file that did not load; empty when all load."""
    out: dict[str, str] = {}
    for read in _folders(directory, paths):
        out.update(read.problems)
    return out


def load_raw(format_id: str, directory: Path | None = None, *, paths: Paths | None = None) -> dict[str, Any]:
    """The mapping as written in the file behind `format_id` (the owner's file wins over the shipped one).

    A `Format` validated from it knows which values the file wrote (`written`); a `model_dump` round trip
    would lose that, so jobs store this mapping.
    """
    found: dict[str, Any] | None = None
    for read in _folders(directory, paths):
        if format_id in read.raw:
            found = read.raw[format_id]
    if found is None:
        known = sorted(load_formats(directory, paths=paths))
        raise StudioError("NS-E320", f"unknown format {format_id!r}; known formats: {known}")
    return found


def broken_overrides(format_ids: Collection[str], *, paths: Paths | None = None) -> dict[str, str]:
    """{id: what is wrong} for the given shipped ids whose owner file does not load.

    `get_format` then quietly returns the shipped preset; callers that can tell the owner say so.
    """
    shipped = _load_dir(str(FORMATS_DIR)).formats
    problems = load_problems(paths=paths)
    return {fid: problems[fid] for fid in sorted(set(format_ids)) if fid in shipped and fid in problems}


def shipped_raw(format_id: str) -> dict[str, Any]:
    """The shipped file's mapping for `format_id`, whatever the owner edited."""
    raw = _load_dir(str(FORMATS_DIR)).raw.get(format_id)
    if raw is None:
        raise StudioError("NS-E320", f"{format_id!r} is not a shipped format")
    return raw


def format_source(format_id: str, *, paths: Paths | None = None) -> str:
    """`shipped` (the preset as shipped), `edited` (the owner's file replaces a shipped id) or `yours`."""
    shipped = _load_dir(str(FORMATS_DIR)).formats
    mine = _load_dir(str((paths or Paths.from_env()).formats_dir)).formats
    if format_id in mine:
        return "edited" if format_id in shipped else "yours"
    return "shipped"


def get_format(format_id: str, directory: Path | None = None, *, paths: Paths | None = None) -> Format:
    """One preset by id; NS-E320 naming the file's own problem (or the known ids) when it does not load."""
    formats = load_formats(directory, paths=paths)
    if format_id in formats:
        return formats[format_id]
    problem = load_problems(directory, paths=paths).get(format_id)
    if problem:
        raise StudioError("NS-E320", problem)
    raise StudioError("NS-E320", f"unknown format {format_id!r}; known formats: {sorted(formats)}")


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
    if fmt.canvas.w % 2 or fmt.canvas.h % 2:
        out.append(
            f"canvas {fmt.canvas.w}x{fmt.canvas.h}: width and height must be even "
            "(H.264 cannot encode odd sizes)"
        )
    if fmt.duration.min_s > fmt.duration.max_s:
        out.append("duration.min_s is larger than duration.max_s")
    if fmt.shots is not None and fmt.shots.min_s > fmt.shots.max_s:
        out.append("shots.min_s is larger than shots.max_s")
    variant = variant_for(fmt)
    top, bottom = scaled_margins(
        fmt.safe_margins.top, fmt.safe_margins.bottom, variant, (fmt.canvas.w, fmt.canvas.h)
    )
    if top + bottom >= fmt.canvas.h:
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


# ---------- what a format may be used for ----------
_CLIP_KINDS: dict[tuple[str, ...], str] = {
    ("facecam", "full"): "gaming",
    ("facecam", "screen"): "screen_cam",
    ("face:0", "face:1"): "stacked",
    ("face:0",): "tracked",
    ("full",): "center",
}


def clip_layout_kind(fmt: Format) -> str | None:
    """The clipper's layout kind a format's regions describe (Head decision D4: derived, not stored).

    gaming (facecam + full), screen_cam (facecam + screen), stacked (face:0 + face:1), tracked (face:0),
    center (full, with an optional blurred fill). None when no clip planner can lay the regions out.
    """
    sources = tuple(sorted(r.source for r in fmt.layout.regions if r.source != "blur_fill"))
    return _CLIP_KINDS.get(sources)


def story_role_problems(fmt: Format) -> list[str]:
    """Why a format cannot draw a story Short or a master (empty = it can).

    The footage of a story video is one full-frame picture: every region shows `full` or a `blur_fill` of it,
    and exactly one is the picture itself.
    """
    out: list[str] = []
    if fmt.kind not in STORY_KINDS:
        out.append(f"a {fmt.kind} format draws clips, not Shorts or full videos")
    odd = sorted({r.source for r in fmt.layout.regions if r.source not in ("full", "blur_fill")})
    if odd:
        out.append(f"region sources {odd} need a camera or a speaker; a story video has one picture (full)")
    fulls = [r for r in fmt.layout.regions if r.source == "full"]
    if len(fulls) != 1:
        out.append(f"exactly one region must show the picture (source full), found {len(fulls)}")
    return out + format_problems(fmt, set(load_caption_styles()))


def clip_layout_problems(fmt: Format) -> list[str]:
    """Why a format cannot be a clip layout (empty = it can)."""
    out: list[str] = []
    if fmt.kind != "clip":
        out.append(f"a {fmt.kind} format is not a clip layout")
    if clip_layout_kind(fmt) is None:
        out.append("its regions match none of the clip layouts (gaming, screen, stacked, tracked, centre)")
    return out + format_problems(fmt, set(load_caption_styles()))


def definition_problems(raw: Any, stem: str | None = None) -> list[str]:
    """Plain problems with a format mapping: the model first, then the studio's own rules for its kind.

    The model (`ns_common.models.Format`) is the single validator (Head decision D3): a field it refuses is
    named with its path. A story format must also be usable for the role its kind fills; a clip format as a
    clip layout. `stem` is the name of the file the mapping was read from: the loader refuses a file whose
    `id` is not its name, so a caller that read a file passes it.
    """
    if not isinstance(raw, dict):
        return ["a format must be a mapping of settings"]
    try:
        fmt = Format.model_validate(raw)
    except ValidationError as e:
        return fields_problem(e).split("; ")
    if stem is not None and fmt.id != stem:
        return [f"id {fmt.id!r} must equal the file name"]
    return kind_problems(fmt)


def kind_problems(fmt: Format) -> list[str]:
    """Why a loaded format cannot do what its kind is for (empty = it can).

    A short, long or square format must be able to draw a story video; a clip format a clip layout.
    """
    return story_role_problems(fmt) if fmt.kind in STORY_KINDS else clip_layout_problems(fmt)


def story_use_problems(format_id: str, *, paths: Paths | None = None) -> list[str]:
    """Why `format_id` cannot draw a Short or a full video (empty = it can).

    The id is unknown, its file does not load (the file's own problem is named), or the studio refuses it
    for a story video (a clip layout, a region off the canvas, a shot range that is upside down, ...). This is
    what a series design is checked with when it is saved and when a part is queued.
    """
    try:
        fmt = get_format(format_id, paths=paths)
    except StudioError as e:
        return [e.detail]
    return story_role_problems(fmt)


def any_use_problems(format_id: str, *, paths: Paths | None = None) -> list[str]:
    """Why `format_id` is of no use to a channel (empty = a video or a clip layout can be drawn from it)."""
    try:
        fmt = get_format(format_id, paths=paths)
    except StudioError as e:
        return [e.detail]
    return kind_problems(fmt)


def declared_kind(format_id: str, *, paths: Paths | None = None) -> str | None:
    """The `kind` a format's file says, even when the file does not load as a whole; None when it says none.

    A series design that names a format whose file is broken still has a kind in mind (the design's `short`
    format shapes the Shorts). Reading it lets the fallback choose a format of the same kind.
    """
    owner = (paths or Paths.from_env()).formats_dir
    for folder in (owner, FORMATS_DIR):
        path = folder / f"{format_id}.yaml"
        try:
            raw = yaml.safe_load(read_text_retry(path))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(raw, dict) and isinstance(raw.get("kind"), str):
            return raw["kind"]
    return None


# ---------- what a render takes from a format ----------
def written(fmt: BaseModel, dotted: str) -> bool:
    """True when the file the model was validated from wrote `dotted` (e.g. `captions.anchor_y_pct`).

    A value the file leaves out is a default of the model, and a default never changes a render: the source
    the render uses today applies. Only a model validated from the file's own mapping knows (a `model_dump`
    round trip marks every field as written).
    """
    obj: Any = fmt
    for part in dotted.split("."):
        if not isinstance(obj, BaseModel) or part not in obj.model_fields_set:
            return False
        obj = getattr(obj, part)
        if obj is None:
            return False
    return True


def variant_for(fmt: Format) -> Variant:
    """The caption geometry a canvas uses: `vertical` unless it is wider than tall (a square is vertical)."""
    return "vertical" if fmt.canvas.h >= fmt.canvas.w else "landscape"


#: The frame the caption styles' pixel sizes and the platform safe zones are written for, per variant. This is
#: the one place the two sizes are written: captions, safe margins, QC and the renderer's `VERTICAL` and
#: `LANDSCAPE` all read it.
REFERENCE_CANVAS: dict[Variant, tuple[int, int]] = {"vertical": (1080, 1920), "landscape": (1920, 1080)}


def reference_canvas(variant: Variant) -> tuple[int, int]:
    """The canvas the caption styles' pixel geometry is written for."""
    return REFERENCE_CANVAS[variant]


def canvas_scale(variant: Variant, canvas: tuple[int, int] | None) -> float:
    """How much larger a frame is than the reference frame of its variant (width over reference width).

    Font sizes, side margins, outlines and the safe margins of a format are pixels at the reference size; on
    a frame of another size they are multiplied by this. A frame without a size of its own (None) and the
    reference frame itself give exactly 1.
    """
    if canvas is None:
        return 1.0
    return canvas[0] / REFERENCE_CANVAS[variant][0]


def scaled_margins(
    top: int, bottom: int, variant: Variant, canvas: tuple[int, int] | None
) -> tuple[int, int]:
    """(top, bottom) safe margins, written in pixels at the reference frame, as pixels of `canvas`."""
    k = canvas_scale(variant, canvas)
    if k == 1.0:
        return top, bottom
    return round(top * k), round(bottom * k)


def footage_orientation(fmt: Format) -> Orientation:
    """The footage to search for: `portrait` for a canvas taller than wide, else `landscape`."""
    return "portrait" if fmt.canvas.h > fmt.canvas.w else "landscape"


def fps_of(fmt: Format, default: int) -> int:
    """The format's frame rate when its file writes one, else `default` (Settings, or 30 for clips)."""
    return fmt.canvas.fps if written(fmt, "canvas.fps") else default


def encoder_preset_of(fmt: Format, default: str) -> str:
    """The format's encoder preset when its file writes one, else `default` (the runtime's)."""
    if fmt.encoder is not None and written(fmt, "encoder.preset"):
        return fmt.encoder.preset
    return default


def shot_range(treatment_range: tuple[float, float], fmt: Format | None) -> tuple[float, float]:
    """The visual treatment's cut range limited to the format's `shots` range.

    No `shots` in the file: the treatment's range. The overlap of the two when there is one; the format's own
    range when they do not meet.
    """
    if fmt is None or fmt.shots is None:
        return treatment_range
    t_lo, t_hi = treatment_range
    f_lo, f_hi = fmt.shots.min_s, fmt.shots.max_s
    lo, hi = max(t_lo, f_lo), min(t_hi, f_hi)
    return (lo, hi) if lo <= hi else (f_lo, f_hi)


def style_for_format(fmt: Format, style_name: str) -> tuple[str, bool]:
    """(caption style, changed): `style_name`, or the first style the format's file allows."""
    if written(fmt, "captions.styles_allowed") and fmt.captions.styles_allowed:
        if style_name not in fmt.captions.styles_allowed:
            return fmt.captions.styles_allowed[0], True
    return style_name, False


# ---------- channel option `formats_allowed` ----------
def formats_allowed(channel: Channel) -> list[str] | None:
    """The channel's `formats_allowed` list; None (every format allowed) when it is empty."""
    return list(channel.formats_allowed) or None


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
