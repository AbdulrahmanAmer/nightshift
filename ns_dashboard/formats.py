"""Format presets (spec 17.1): loading from the shipped and user folders, validating, writing overrides.

The schema is `ns_common.models.Format`. User edits live in `<home>/config/formats/<id>.yaml` and shadow the
shipped presets of the same id (`ns_studio/presets/formats/`).
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ns_common.atomic import write_text_atomic
from ns_common.models import Format
from ns_common.paths import Paths
from ns_dashboard.errors import ApiError

ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_format(definition: Any) -> list[str]:
    """Plain problems with a format definition (empty list = valid), checked by `ns_common.models.Format`."""
    if not isinstance(definition, dict):
        return ["a format must be a mapping"]
    try:
        Format.model_validate(definition)
    except ValidationError as e:
        out = []
        for err in e.errors():
            where = ".".join(str(p) for p in err["loc"]) or "format"
            kind = err["type"]
            if kind == "missing":
                out.append(f"{where} is required")
            elif kind == "extra_forbidden":
                out.append(f"{where} is not a known setting")
            else:
                out.append(f"{where}: {err['msg']}")
        return out
    problems = []
    dur = definition["duration"]
    if dur["max_s"] < dur["min_s"]:
        problems.append("duration.max_s must not be smaller than duration.min_s")
    return problems


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
    return paths.formats_dir


def _load(file: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _canvas_summary(canvas: Any) -> Any:
    """The canvas for the format cards: a file that leaves `fps` out (the shipped presets since P10a) shows
    the model's default, which is also what a render uses when Settings has no frame rate of its own."""
    if isinstance(canvas, dict) and "fps" not in canvas:
        return {**canvas, "fps": 30}
    return canvas


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
                "name": d.get("name") or fid.replace("_", " ").title(),
                "kind": d.get("kind"),
                "canvas": _canvas_summary(d.get("canvas")),
                "source": entry["source"],
                "valid": not errors,
                "errors": errors,
                "definition": d,
            }
        )
    return out


def _write(paths: Paths, fid: str, definition: dict[str, Any]) -> None:
    text = yaml.safe_dump(definition, sort_keys=False, allow_unicode=True)
    write_text_atomic(user_dir(paths) / f"{fid}.yaml", text)


def merge_dicts(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Deep merge: dicts merge key by key, everything else is replaced."""
    out = copy.deepcopy(base)
    for k, v in changes.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_dicts(out[k], v)
        else:
            out[k] = v
    return out


def create_format(paths: Paths, body: dict[str, Any]) -> dict[str, Any]:
    """New format from `definition`, or a copy of `duplicate_of` (optionally with `changes`)."""
    new_id, definition = check_create_format(paths, body)
    _write(paths, new_id, definition)
    return next(f for f in list_formats(paths) if f["id"] == new_id)


def check_create_format(paths: Paths, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The id and definition `create_format` would write, or the ApiError it would raise (writes nothing)."""
    existing = {f["id"]: f for f in list_formats(paths)}
    source_id = body.get("duplicate_of")
    if source_id:
        if source_id not in existing:
            raise ApiError(404, "not_found", f"There is no format called '{source_id}'.")
        definition = copy.deepcopy(existing[source_id]["definition"])
        definition["name"] = f"{definition.get('name') or source_id} (copy)"
        definition = merge_dicts(definition, body.get("changes") or {})
        new_id = str(body.get("id") or f"{source_id}_copy")
        n = 2
        while not body.get("id") and new_id in existing:
            new_id = f"{source_id}_copy{n}"
            n += 1
    else:
        definition = body.get("definition")
        new_id = str(body.get("id") or (definition or {}).get("id") or "")
        if not isinstance(definition, dict):
            raise ApiError(422, "invalid_format", "Send a 'definition' or a 'duplicate_of'.")
        definition = {**definition}
    if not ID_RE.match(new_id):
        raise ApiError(422, "invalid_format", "The format id must be lowercase letters, digits, underscores.")
    if new_id in existing:
        raise ApiError(409, "format_exists", f"A format called '{new_id}' already exists.")
    definition["id"] = new_id
    problems = validate_format(definition)
    if problems:
        raise ApiError(422, "invalid_format", "That format is not valid.", details=problems)
    return new_id, definition


def update_format(paths: Paths, fid: str, body: dict[str, Any]) -> dict[str, Any]:
    """Replace (`definition`) or patch (`changes`) a format; editing a shipped one writes a user override."""
    definition = check_update_format(paths, fid, body)
    _write(paths, fid, definition)
    return next(f for f in list_formats(paths) if f["id"] == fid)


def check_update_format(paths: Paths, fid: str, body: dict[str, Any]) -> dict[str, Any]:
    """The definition `update_format` would write, or the ApiError it would raise (nothing is written)."""
    existing = {f["id"]: f for f in list_formats(paths)}
    if fid not in existing:
        raise ApiError(404, "not_found", f"There is no format called '{fid}'.")
    if isinstance(body.get("definition"), dict):
        definition = {**body["definition"]}
    elif isinstance(body.get("changes"), dict):
        definition = merge_dicts(existing[fid]["definition"], body["changes"])
    else:
        raise ApiError(422, "invalid_format", "Send 'definition' or 'changes'.")
    definition["id"] = fid
    problems = validate_format(definition)
    if problems:
        raise ApiError(422, "invalid_format", "That format is not valid.", details=problems)
    return definition
