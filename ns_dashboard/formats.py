"""Format presets (spec 17.1): local schema, loading from the shipped and user folders, writing overrides.

`contracts/schemas/format.schema.json` does not exist yet; `FORMAT_SCHEMA` is the local stand-in.
User edits live in `<home>/config/formats/<id>.yaml` and shadow the shipped presets of the same id.
"""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

from ns_common.paths import Paths
from ns_dashboard.assistant.registry import check_value
from ns_dashboard.errors import ApiError

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
SOURCE_PATTERN = r"^(full|facecam|screen|blur_fill|face:\d+)$"


def _num(lo: float | None = None, hi: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "number"}
    if lo is not None:
        out["minimum"] = lo
    if hi is not None:
        out["maximum"] = hi
    return out


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


FORMAT_SCHEMA: dict[str, Any] = _obj(
    {
        "label": {"type": "string", "maxLength": 80},
        "description": {"type": "string", "maxLength": 400},
        "kind": {"type": "string", "enum": ["short", "long", "square", "clip"]},
        "canvas": _obj(
            {
                "w": {"type": "integer", "minimum": 16, "maximum": 8192},
                "h": {"type": "integer", "minimum": 16, "maximum": 8192},
                "fps": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            ["w", "h", "fps"],
        ),
        "duration": _obj(
            {"min_s": _num(0), "max_s": _num(1), "buckets": {"type": "array", "items": _num(1)}},
            ["min_s", "max_s"],
        ),
        "captions": _obj(
            {
                "styles_allowed": {"type": "array", "items": {"type": "string"}},
                "anchor_y_pct": _num(0, 100),
                "words_on_screen": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            [],
        ),
        "layout": _obj(
            {
                "regions": {
                    "type": "array",
                    "items": _obj(
                        {
                            "name": {"type": "string"},
                            "x": _num(0),
                            "y": _num(0),
                            "w": _num(0),
                            "h": _num(0),
                            "source": {"type": "string", "pattern": SOURCE_PATTERN},
                        },
                        ["name", "x", "y", "w", "h", "source"],
                    ),
                },
                "seam_captions": {"type": "boolean"},
            },
            ["regions"],
        ),
        "safe_margins": _obj({"top": _num(0), "bottom": _num(0)}, []),
        "shots": _obj({"min_s": _num(0), "max_s": _num(0)}, []),
        "encoder": _obj({"preset": {"type": "string"}}, []),
        "sheet": _obj(
            {"hashtags_required": {"type": "array", "items": {"type": "string"}}, "description_rules": {"type": "string"}},
            [],
        ),
        "channels_allowed": {"type": "array", "items": {"type": "string"}},
    },
    ["kind", "canvas", "duration", "layout"],
)


def validate_format(definition: Any) -> list[str]:
    """Human-readable problems with a format definition (empty list = valid)."""
    if not isinstance(definition, dict):
        return ["a format must be a mapping"]
    errors: list[str] = []
    bad = check_value(FORMAT_SCHEMA, definition, "format")
    if bad:
        errors.append(bad)
    regions = (definition.get("layout") or {}).get("regions") if isinstance(definition.get("layout"), dict) else None
    if isinstance(regions, list) and not regions:
        errors.append("layout.regions needs at least one region")
    dur = definition.get("duration")
    if isinstance(dur, dict) and isinstance(dur.get("min_s"), int | float) and isinstance(dur.get("max_s"), int | float):
        if dur["max_s"] < dur["min_s"]:
            errors.append("duration.max_s must not be smaller than duration.min_s")
    return errors


def builtin_dir() -> Path | None:
    """`ns_studio/presets/formats` once the studio ships it."""
    try:
        import ns_studio
    except ImportError:
        return None
    d = Path(ns_studio.__file__).resolve().parent / "presets" / "formats"
    return d if d.is_dir() else None


def user_dir(paths: Paths) -> Path:
    """Where edited and duplicated formats are stored."""
    return paths.config / "formats"


def _load(file: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def list_formats(paths: Paths) -> list[dict[str, Any]]:
    """Every format (shipped and user) with its validation state; user files shadow shipped ones."""
    found: dict[str, dict[str, Any]] = {}
    b = builtin_dir()
    if b is not None:
        for f in sorted(b.glob("*.yaml")):
            d = _load(f)
            if d is not None:
                found[f.stem] = {"source": "shipped", "definition": d}
    u = user_dir(paths)
    if u.is_dir():
        for f in sorted(u.glob("*.yaml")):
            d = _load(f)
            if d is not None:
                found[f.stem] = {"source": "edited" if f.stem in found else "yours", "definition": d}
    out = []
    for fid, entry in sorted(found.items()):
        d = entry["definition"]
        errors = validate_format(d)
        out.append(
            {
                "id": fid,
                "label": d.get("label") or fid.replace("_", " ").title(),
                "kind": d.get("kind"),
                "canvas": d.get("canvas"),
                "source": entry["source"],
                "valid": not errors,
                "errors": errors,
                "channels_allowed": d.get("channels_allowed", []),
                "definition": d,
            }
        )
    return out


def _write(paths: Paths, fid: str, definition: dict[str, Any]) -> None:
    d = user_dir(paths)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{fid}.yaml").write_text(yaml.safe_dump(definition, sort_keys=False, allow_unicode=True), encoding="utf-8")


def merge_dicts(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in changes.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_dicts(out[k], v)
        else:
            out[k] = v
    return out


def create_format(paths: Paths, body: dict[str, Any]) -> dict[str, Any]:
    """New format from `definition`, or a copy of `duplicate_of` (optionally with `changes`)."""
    existing = {f["id"]: f for f in list_formats(paths)}
    source_id = body.get("duplicate_of")
    if source_id:
        if source_id not in existing:
            raise ApiError(404, "not_found", f"There is no format called '{source_id}'.")
        definition = copy.deepcopy(existing[source_id]["definition"])
        definition["label"] = f"{definition.get('label') or source_id} (copy)"
        definition = merge_dicts(definition, body.get("changes") or {})
        new_id = str(body.get("id") or f"{source_id}_copy")
        n = 2
        while not body.get("id") and new_id in existing:
            new_id = f"{source_id}_copy{n}"
            n += 1
    else:
        definition = body.get("definition")
        new_id = str(body.get("id") or "")
        if not isinstance(definition, dict):
            raise ApiError(422, "invalid_format", "Send a 'definition' or a 'duplicate_of'.")
    if not ID_RE.match(new_id):
        raise ApiError(422, "invalid_format", "The format id must be lowercase letters, digits and underscores.")
    if new_id in existing:
        raise ApiError(409, "format_exists", f"A format called '{new_id}' already exists.")
    problems = validate_format(definition)
    if problems:
        raise ApiError(422, "invalid_format", "That format is not valid.", details=problems)
    _write(paths, new_id, definition)
    return next(f for f in list_formats(paths) if f["id"] == new_id)


def update_format(paths: Paths, fid: str, body: dict[str, Any]) -> dict[str, Any]:
    """Replace (`definition`) or patch (`changes`) a format; editing a shipped one writes a user override."""
    existing = {f["id"]: f for f in list_formats(paths)}
    if fid not in existing:
        raise ApiError(404, "not_found", f"There is no format called '{fid}'.")
    if isinstance(body.get("definition"), dict):
        definition = body["definition"]
    elif isinstance(body.get("changes"), dict):
        definition = merge_dicts(existing[fid]["definition"], body["changes"])
    else:
        raise ApiError(422, "invalid_format", "Send 'definition' or 'changes'.")
    problems = validate_format(definition)
    if problems:
        raise ApiError(422, "invalid_format", "That format is not valid.", details=problems)
    _write(paths, fid, definition)
    return next(f for f in list_formats(paths) if f["id"] == fid)
