"""Which format a job's videos are drawn from (package P10a, formats as data in the renderer).

`prepare_job_formats` runs once at the start of `run_job`: it picks the format of each video kind the job
makes (`short`, later `long`), copies the format's own file into the job folder (`formats.json`) and logs
the choice and the reason. Every stage then reads the job's copy through `job_format`, so an edit made while
the job runs, or a resume after an update, never changes half a video.

The choice, in this order (Head decision D1):

1. an explicit series design format (`series_context.json["design"]["format"]`): a `short` format shapes the
   part's Shorts, a `long` or `square` one its full video. A design format that is missing, does not load
   or cannot be drawn does not stop the part (it rendered before formats were read): it is skipped, the
   next candidate is used and the job's record says so (`note`, and `why`);
2. the channel's `formats_allowed`, in list order: the first allowed format of the kind ("first allowed of
   that kind"), else the first allowed `square` one. After a design format of the role's own kind was
   skipped there is no square fallback: a Short the design meant to be 9:16 does not turn square, nor a
   16:9 full video;
3. the default (`short_9x16`, `long_16x9`), read through the loader, so the owner's edit of the shipped file
   counts.

Values are stored as the file wrote them (the raw mapping, never a `model_dump`), so `formats.written` can
still tell a value the owner chose from a default.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ns_common.atomic import write_text_atomic
from ns_common.logging import NsLogger
from ns_common.models import Channel, Format
from ns_common.paths import Paths
from ns_studio.errors import StudioError
from ns_studio.media import formats as F

if TYPE_CHECKING:
    from ns_studio.context import JobContext

JOB_FORMATS_FILE = "formats.json"
DEFAULT_IDS = {"short": "short_9x16", "long": "long_16x9"}
#: Video kinds whose renders draw from their format: Shorts since P10a, full videos (masters and series
#: parts) since P10b.
ROLES_APPLIED: tuple[str, ...] = ("short", "long")
#: Roles recorded per job format (`JobContext.fmt`); clips and series compilations choose per clip.
ROLES_BY_PLAN: dict[str, tuple[str, ...]] = {
    "shorts_only": ("short",),
    "master_first": ("short", "long"),
}
NO_ROLES = ("clip", "series_compilation")
_PRIMARY_KINDS = {"short": ("short",), "long": ("long",)}


@dataclass(frozen=True)
class Resolved:
    """The format chosen for one video kind, with the reason."""

    role: str
    id: str
    why: str
    source: str
    raw: dict[str, Any]
    note: str | None = None
    legacy: bool = False

    def entry(self) -> dict[str, Any]:
        """The `formats.json` entry of this role."""
        out: dict[str, Any] = {"id": self.id, "source": self.source, "why": self.why}
        if self.note:
            out["note"] = self.note
        if self.legacy:
            out["legacy"] = True
        out["definition"] = self.raw
        return out


def roles_for(job_format: str) -> tuple[str, ...]:
    """The video kinds a job of this format records (an unknown plan name, e.g. a test's, records `short`)."""
    if job_format in NO_ROLES:
        return ()
    return ROLES_BY_PLAN.get(job_format, ("short",))


def _warn(log: NsLogger | None, event: str, msg: str, **data: Any) -> None:
    if log is not None:
        log.warn(event, msg, **data)


def _finish(
    role: str,
    fid: str,
    why: str,
    paths: Paths | None,
    log: NsLogger | None,
) -> Resolved:
    """A `Resolved` for `fid` from the loader; a broken owner file behind a shipped id is reported."""
    raw = copy.deepcopy(F.load_raw(fid, paths=paths))
    note = None
    problem = F.load_problems(paths=paths).get(fid)
    if problem:
        note = f"your file for {fid} does not load ({problem}); the shipped one is used"
        _warn(log, "format_override_broken", note, role=role, format_id=fid)
    return Resolved(role, fid, why, F.format_source(fid, paths=paths), raw, note)


def _design_pick(
    role: str,
    design_id: str,
    primary: tuple[str, ...],
    paths: Paths | None,
    log: NsLogger | None,
) -> tuple[Resolved | None, str | None, str | None]:
    """(the design's format, None, None) when it can draw `role`; (None, why not, its kind) when unusable.

    (None, None, None) means the design's format is simply for the other kind of video (a `long` one has no
    Short to shape): nothing to report. A format that is unknown, does not load or cannot be drawn is
    reported with the reason; the caller carries on with the next candidate, as for `formats_allowed`. The
    kind is the one the format's file declares (None when even that cannot be read).
    """
    kind: str | None
    try:
        fmt = F.get_format(design_id, paths=paths)
    except StudioError as e:
        reason = e.detail
        kind = F.declared_kind(design_id, paths=paths)
    else:
        kind = fmt.kind
        if not (fmt.kind in primary or (role == "long" and fmt.kind == "square")):
            return None, None, None
        problems = F.story_role_problems(fmt)
        if not problems:
            return _finish(role, design_id, "series design", paths, log), None, None
        reason = "; ".join(problems)
    reason = reason[:300]
    _warn(
        log,
        "format_design_skipped",
        f"the series design names format {design_id!r}, which cannot be used ({reason}); "
        "the next candidate is used instead",
        role=role,
        format_id=design_id,
    )
    return None, reason, kind


def resolve_role(
    role: str,
    allowed: Sequence[str],
    design_id: str | None = None,
    *,
    paths: Paths | None = None,
    log: NsLogger | None = None,
) -> Resolved:
    """The format for `role` (`short` or `long`): series design, first allowed of the kind, then the default.

    A series design format that is unknown, does not load or cannot be drawn is skipped with a warning (a
    part that renders without a format must not fail because of one) and the entry's `why` and `note` say
    so. A format in `formats_allowed` that does not load or cannot be drawn is skipped the same way; when
    nothing in a non-empty list fits, the default is used and `format_role_not_allowed` is logged.
    """
    skipped: str | None = None
    squares_ok = True
    if design_id:
        picked, skipped, kind = _design_pick(role, design_id, _PRIMARY_KINDS[role], paths, log)
        if picked is not None:
            return picked
        # the owner chose a format of this role's own kind (or one whose kind cannot be told): if it cannot be
        # drawn, a square the list happens to hold is not what they asked for. A skipped square design may
        # fall back to another square, and a design for the other kind leaves this role as it was.
        squares_ok = skipped is None or not (kind is None or kind in _PRIMARY_KINDS[role])
    res = _resolve_without_design(role, allowed, paths=paths, log=log, squares_ok=squares_ok)
    if skipped is None:
        return res
    said = f"series design format {design_id!r} cannot be used ({skipped})"
    return replace(res, why=f"{res.why}; {said}", note=f"{said}; {res.note}" if res.note else said)


def _resolve_without_design(
    role: str,
    allowed: Sequence[str],
    *,
    paths: Paths | None,
    log: NsLogger | None,
    squares_ok: bool = True,
) -> Resolved:
    """The first allowed format of the kind, else the first allowed square one, else the default.

    `squares_ok` False leaves the square formats out (a design format of the role's own kind was skipped)."""
    primary = _PRIMARY_KINDS[role]
    formats = F.load_formats(paths=paths)
    problems_by_id = F.load_problems(paths=paths)
    firsts: list[str] = []
    squares: list[str] = []
    for fid in allowed:
        fmt = formats.get(fid)
        if fmt is None:
            _warn(
                log,
                "format_skipped",
                f"{fid}: {problems_by_id.get(fid, 'no such format')}",
                role=role,
                format_id=fid,
            )
            continue
        if fmt.kind == "clip" or not (fmt.kind in primary or fmt.kind == "square"):
            continue  # a layout for clips, or the other kind of video: not for this role, not a problem
        problems = F.story_role_problems(fmt)
        if problems:
            _warn(log, "format_skipped", f"{fid}: {'; '.join(problems)}", role=role, format_id=fid)
            continue
        (firsts if fmt.kind in primary else squares).append(fid)
    pick = (firsts or (squares if squares_ok else []) or [None])[0]
    if pick is not None:
        why = "first allowed of that kind" if firsts else "first allowed square format"
        return _finish(role, pick, why, paths, log)
    default = DEFAULT_IDS[role]
    why = "default (formats_allowed is empty)"
    if allowed:
        why = f"default (no allowed format fits the {role} video)"
        _warn(
            log,
            "format_role_not_allowed",
            f"none of formats_allowed {list(allowed)} fits the {role} video; {default} is used",
            role=role,
            allowed=list(allowed),
        )
    fmt = formats.get(default)
    problems = F.story_role_problems(fmt) if fmt is not None else ["it is missing"]
    if problems:
        shipped = F.shipped_raw(default)
        note = f"your {default} cannot be drawn ({'; '.join(problems)}); the shipped one is used"
        _warn(log, "format_override_broken", note, role=role, format_id=default)
        return Resolved(role, default, why, "shipped", copy.deepcopy(shipped), note)
    return _finish(role, default, why, paths, log)


def warn_broken_overrides(
    format_ids: Collection[str], *, paths: Paths | None, log: NsLogger | None, role: str = "clip"
) -> list[str]:
    """Log `format_override_broken` for each shipped id whose owner file does not load; returns those ids.

    A clip job reads its layout by id, and a hand-broken copy of a clip preset used to be a loud NS-E320;
    now the shipped preset is used, so the job log must say that the owner's file was not.
    """
    broken = F.broken_overrides(format_ids, paths=paths)
    for fid, problem in broken.items():
        _warn(
            log,
            "format_override_broken",
            f"your file for {fid} does not load ({problem}); the shipped one is used",
            role=role,
            format_id=fid,
        )
    return list(broken)


def shipped_entry(role: str) -> Resolved:
    """The shipped definition of the role's default format (what a job from before the update used)."""
    fid = DEFAULT_IDS[role]
    return Resolved(
        role,
        fid,
        "job started before this update: the shipped format is kept",
        "shipped",
        copy.deepcopy(F.shipped_raw(fid)),
        legacy=True,
    )


def _has_done_stage(job_dir: Path) -> bool:
    from ns_studio.stages import StageState

    try:
        entries = StageState.load(job_dir).entries
    except (OSError, ValueError):
        return False
    return any(isinstance(e, dict) and e.get("status") == "done" for e in entries.values())


_REDO = (
    "the job's record of its formats is damaged. A job with no finished stage chooses again when the file "
    "is deleted; a job with finished stages would then fall back to the shipped formats, so start it again "
    "from the beginning instead"
)


def _validate_doc(doc: Any, roles: Sequence[str]) -> dict[str, Any]:
    """`formats.json` checked: every wanted role present and valid (NS-E320 naming the field)."""
    if not isinstance(doc, dict):
        raise StudioError("NS-E320", f"{JOB_FORMATS_FILE} is not a mapping; {_REDO}")
    for role in roles:
        entry = doc.get(role)
        if not isinstance(entry, dict) or not isinstance(entry.get("definition"), dict):
            raise StudioError("NS-E320", f"{JOB_FORMATS_FILE} has no {role} format; {_REDO}")
        try:
            Format.model_validate(entry["definition"])
        except ValidationError as e:
            raise StudioError(
                "NS-E320", f"{JOB_FORMATS_FILE} {role} format ({entry.get('id')}): {F.fields_problem(e)}"
            ) from e
    return doc


def _design_id(ctx: JobContext) -> str | None:
    """The format a series part's design names (`series_context.json["design"]["format"]`), if any."""
    design = ctx.series.get("design") if ctx.series else None
    if isinstance(design, dict) and isinstance(design.get("format"), str):
        return design["format"]
    return None


def _choose(ctx: JobContext, role: str, *, legacy: bool) -> Resolved:
    """The format of `role` for this job: the shipped one for a job from before the update, else chosen."""
    if legacy:
        return shipped_entry(role)
    return resolve_role(role, ctx.channel.formats_allowed, _design_id(ctx), paths=ctx.paths, log=ctx.log)


def _log_choice(ctx: JobContext, res: Resolved) -> None:
    ctx.log.info(
        "job_format",
        f"{res.role} videos use {res.id} ({res.why})",
        job_id=ctx.job_id,
        role=res.role,
        format_id=res.id,
        why=res.why,
        source=res.source,
    )


#: Roles a later package added to the record: a job that an earlier one started lacks them and is not damaged.
LATER_ROLES: tuple[str, ...] = ("long",)
ADDED_NOTE = (
    "this job started before full videos read their format; "
    "the full video's format was chosen when it resumed"
)


def _add_missing_roles(ctx: JobContext, doc: dict[str, Any], roles: Sequence[str]) -> dict[str, Any]:
    """`doc` with an entry for each wanted role it lacks (a job that P10a started has no `long` one).

    A job that already finished a stage keeps the shipped definition (what it drew before the update), so the
    captions it wrote and the render that follows agree; a job that has not drawn anything yet chooses as a
    new job would. The entry says it was added. The record is rewritten whole, atomically.
    """
    missing = [r for r in roles if r in LATER_ROLES and not isinstance(doc.get(r), dict)]
    earlier = [r for r in roles if r not in LATER_ROLES]
    if not missing or doc.get("version") != 1 or not all(isinstance(doc.get(r), dict) for r in earlier):
        return doc  # nothing to add, or a damaged record: `_validate_doc` says what is wrong
    legacy = _has_done_stage(ctx.job_dir)
    out = dict(doc)
    for role in missing:
        res = _choose(ctx, role, legacy=legacy)
        if not res.legacy:
            res = replace(res, note=f"{ADDED_NOTE}; {res.note}" if res.note else ADDED_NOTE)
        out[role] = res.entry()
        _log_choice(ctx, res)
    write_text_atomic(ctx.file(JOB_FORMATS_FILE), json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return out


def prepare_job_formats(ctx: JobContext) -> dict[str, Any] | None:
    """Choose, record and log the job's formats once; later calls read the record (None: this job has none).

    A job that already has completed stages and no `formats.json` was started before the update that brought
    formats into the renderer: it keeps the shipped definitions, so the captions it wrote and the render that
    follows agree. A record that lacks a video kind the job now draws (a full video, for a job P10a started)
    gets that entry added when the job resumes.
    """
    roles = roles_for(ctx.fmt)
    if not roles:
        return None
    f = ctx.file(JOB_FORMATS_FILE)
    if f.exists():
        doc = ctx.read_json(JOB_FORMATS_FILE)
        if isinstance(doc, dict):
            doc = _add_missing_roles(ctx, doc, roles)
        return _validate_doc(doc, roles)
    from ns_studio.media.format_notice import notify_owner_formats

    notify_owner_formats(ctx.paths, ctx.log)
    legacy = _has_done_stage(ctx.job_dir)
    fresh: dict[str, Any] = {"version": 1}
    for role in roles:
        res = _choose(ctx, role, legacy=legacy)
        fresh[role] = res.entry()
        _log_choice(ctx, res)
    write_text_atomic(f, json.dumps(fresh, indent=2, ensure_ascii=False) + "\n")
    return fresh


def job_format(ctx: JobContext, role: str) -> Format:
    """The job's format for `role`, validated from the job's own record (written first when it is missing)."""
    doc = prepare_job_formats(ctx)
    entry = (doc or {}).get(role)
    if not isinstance(entry, dict):
        raise StudioError("NS-E320", f"a {ctx.fmt} job has no {role} format")
    return Format.model_validate(entry["definition"])


def job_format_entry(ctx: JobContext, role: str) -> dict[str, Any]:
    """The `formats.json` entry (id, source, why) of `role`, for reports; empty when the job has none."""
    doc = prepare_job_formats(ctx)
    entry = (doc or {}).get(role)
    return entry if isinstance(entry, dict) else {}


def formats_in_use(channel: Channel, paths: Paths) -> dict[str, dict[str, Any]]:
    """What a new job of `channel` would draw each video kind from (Head decision D1), for the channel API.

    Never raises: a problem is reported in the entry. `applied` is False for a kind whose renders do not
    draw from the format yet.
    """
    out: dict[str, dict[str, Any]] = {}
    for role in ("short", "long"):
        try:
            res = resolve_role(role, list(channel.formats_allowed), None, paths=paths)
            out[role] = {
                "id": res.id,
                "name": str(res.raw.get("name") or res.id),
                "why": res.why,
                "source": res.source,
                "applied": role in ROLES_APPLIED,
            }
            if res.note:
                out[role]["problem"] = res.note
        except StudioError as e:
            out[role] = {"id": None, "problem": e.detail, "applied": role in ROLES_APPLIED}
    return out
