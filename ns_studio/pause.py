"""A paused channel starts no stage (P47): the one check every route of a story job passes through.

The invariant: no stage of a paused channel's story job starts, and nothing is lost or duplicated when the
channel is resumed. `orchestrator.run_job` calls `require_not_paused` before it takes a GPU tenant for a
group of stages and before every stage, so the dashboard's studio child (a new video, a Retry, a series part),
`studio run` and the nightly run all stop at the same place: after the stage under way, before the next. A
stage is never cut in the middle (Cancel does that); its outputs and `stage_state.json` stay, so continuing
the job after the resume (`--resume`, Retry, the next night, the dashboard's held job) runs only the stages
that did not run, once.

The earlier looks (the dashboard worker's hold, `studio run --channel` and `--resume`, the nightly's
`_paused_now`) keep a paused channel's job from starting at all; this one closes the gap behind them.
"""

from __future__ import annotations

import yaml
from pydantic import ValidationError

from ns_common.config import load_channel
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_studio.errors import PAUSED


def channel_paused(paths: Paths, slug: str, log: NsLogger | None = None) -> bool:
    """True when the channel's profile says `paused` right now (read again each time).

    No profile file is not a pause and says nothing (a job of a channel that was removed, a test context). A
    profile that cannot be read (torn, invalid, or held open by another program: OSError) is not a pause
    either: a WARN `channel_profile_unreadable`, and the job goes on, as the other looks decide."""
    if not paths.channel_file(slug).exists():
        return False
    try:
        return load_channel(paths, slug).status == "paused"
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as e:
        if log is not None:
            log.warn("channel_profile_unreadable", str(e), channel=slug, check="pause")
        return False


def require_not_paused(
    paths: Paths, log: NsLogger, slug: str, display_name: str, job_id: str, stage: str
) -> None:
    """NS-E330 when the channel is paused now: the job stops before `stage` with its finished stages kept."""
    if not channel_paused(paths, slug, log):
        return
    log.info("job_paused_before_stage", job_id=job_id, channel=slug, stage=stage)
    raise NsError(
        PAUSED,
        f"{display_name} was paused; job {job_id} stopped before its stage {stage}, its finished stages kept",
        job_id=job_id,
        stage=stage,
    )
