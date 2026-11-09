"""The free-space check that runs before a job starts (order D-33, built on ``ns_common.diskneed``).

Before D-33 nothing looked at the drive: a Short on a volume with 20 MB free was accepted, ran the policy
text, the voice and the clips, wrote 19 MB and only then failed in ``clips`` with NS-E104. ``studio run``, the
nightly run and the dashboard now compare the job's estimate with the free space of the volume that holds the
home BEFORE anything is created, and refuse with ``NS-E104`` ("This needs about 100 MB (...); D: has 20 MB
free").

A volume whose free space cannot be read never blocks (``diskneed.check`` logs ``disk_free_unknown``). A
resumed job needs only what its unfinished stages still write (``Need.remaining`` with the stages
``stage_state.json`` says are done).
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from ns_common import diskneed
from ns_common.devmode import fake_footage
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import Channel
from ns_common.paths import Paths


def real_footage(paths: Paths, fake: Collection[str] = ()) -> bool:
    """False when the clips come from the fake clip source (dev install, ``fake_footage``, fake services)."""
    return not ("clips" in fake or fake_footage(paths))


def done_stages(job_dir: Path) -> set[str]:
    """Names of the stages a job folder says are finished (empty when it has no readable state yet)."""
    from ns_studio.stages import StageState

    try:
        entries = StageState.load(job_dir).entries
    except (OSError, ValueError):
        return set()
    if not isinstance(entries, dict):  # valid JSON that is not an object: nothing is known to be done
        return set()
    return {n for n, e in entries.items() if isinstance(e, dict) and e.get("status") == "done"}


def story_need(
    paths: Paths,
    channel: Channel,
    fmt: str,
    *,
    count: int = 1,
    fake: Collection[str] = (),
    resume_dir: Path | None = None,
) -> diskneed.Need | None:
    """What one story job of ``fmt`` for ``channel`` writes; None only for a format name nobody has.

    Every format a job can have (``ns_studio.jobs.Fmt``: also ``master_from_script`` and ``clip``, which a
    resume reads back from ``job.json``) has an estimate; a test walks the Literal so a new one cannot slip
    through unchecked. The channel's own ranges set the size (the longest video it may draw). ``resume_dir``
    (the folder of the job being continued) shrinks the estimate to the stages that are not done, and gives a
    Clipper job its own clip count.
    """
    try:
        need = diskneed.format_need(
            fmt,
            long_minutes=channel.long_minutes,
            shorts_per_master=channel.shorts_per_master,
            count=count,
            real_footage=real_footage(paths, fake),
            max_clips=_clip_count(resume_dir) if fmt == "clip" else 6,
        )
    except ValueError:
        return None
    return need.remaining(done_stages(resume_dir)) if resume_dir is not None else need


def _clip_count(job_dir: Path | None) -> int:
    """How many clips a Clipper job asked for (``clip_params.json``); the default when it cannot be read."""
    if job_dir is None:
        return 6
    from ns_studio.clipper.models import ClipParams

    try:
        return max(1, int(ClipParams.load(job_dir).max_clips))
    except (NsError, OSError, ValueError, TypeError):
        return 6


def require_story_room(
    paths: Paths,
    log: NsLogger | None,
    channel: Channel,
    fmt: str,
    *,
    count: int = 1,
    fake: Collection[str] = (),
    resume_dir: Path | None = None,
) -> diskneed.Verdict | None:
    """NS-E104 (writing nothing) when the home's volume cannot hold one more story job of ``fmt``."""
    need = story_need(paths, channel, fmt, count=count, fake=fake, resume_dir=resume_dir)
    if need is None:
        return None
    return diskneed.require_room(need, paths.home, log=log)


def source_copy_bytes(paths: Paths, source: Path | str | None) -> int:
    """Bytes the Clipper's import copies into the home for a file: its size, 0 when it is already in there.

    A file in the home's ``imports/clipper/<source id>`` folder (an upload that was adopted) is already where
    the import wants it. A video in ``imports/clipper/_downloads`` is not: it was downloaded for the job, and
    the import (``ImportStage`` with a file path, so ``link=False``) copies it to its own folder before the
    download is deleted, so for a moment it is on the drive twice and the copy counts.
    """
    if not source or str(source).lower().startswith(("http://", "https://")):
        return 0
    try:
        p = Path(source)
        clipper = (paths.home / "imports" / "clipper").resolve()
        here = p.resolve()
        if here.is_relative_to(clipper) and not here.is_relative_to(clipper / "_downloads"):
            return 0
        return p.stat().st_size
    except OSError:
        return 0


def clip_need(
    paths: Paths,
    *,
    max_clips: int = 6,
    url_source: bool = False,
    rerun: bool = False,
    source: Path | str | None = None,
    fake: Collection[str] = (),
) -> diskneed.Need:
    """What a Clipper job writes (see ``diskneed.clip_need``); ``source`` is the file it will import."""
    return diskneed.clip_need(
        max_clips,
        url_source=url_source,
        rerun=rerun,
        real_footage=real_footage(paths, fake),
        source_bytes=0 if rerun or url_source else source_copy_bytes(paths, source),
    )


def require_clip_room(
    paths: Paths,
    log: NsLogger | None,
    *,
    max_clips: int = 6,
    url_source: bool = False,
    rerun: bool = False,
    source: Path | str | None = None,
    fake: Collection[str] = (),
) -> diskneed.Verdict:
    """NS-E104 (writing nothing) when the home's volume cannot hold one more Clipper job."""
    need = clip_need(paths, max_clips=max_clips, url_source=url_source, rerun=rerun, source=source, fake=fake)
    return diskneed.require_room(need, paths.home, log=log)


def require_series_part_room(
    paths: Paths,
    log: NsLogger | None,
    *,
    fake: Collection[str] = (),
    resume_dir: Path | None = None,
) -> diskneed.Verdict:
    """NS-E104 (writing nothing) when the home's volume cannot hold a series part (a master and its Shorts).

    The part's own design is not read: it is a master of the channel default's middle length with 5 Shorts.
    A part whose job folder already holds finished stages needs only the rest.
    """
    need = diskneed.format_need("series_part", real_footage=real_footage(paths, fake))
    if resume_dir is not None and (resume_dir / "job.json").exists():
        need = need.remaining(done_stages(resume_dir))
    return diskneed.require_room(need, paths.home, log=log)
