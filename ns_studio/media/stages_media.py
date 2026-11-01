"""Registers the media stages (component C6).

Import this module (the CLI does) to register them. The order per format lives in the orchestrator's
FORMAT_STAGES constants; this module only calls `register`. Names shared by both formats (`voice`,
`captions`, `clips`, `qc`, `outbox`) are single stage objects that dispatch on `ctx.fmt`.

Shorts-only media order: voice, captions, clips, render, qc, outbox. Master-first media order:
voice, duration_check, captions, shorts_timing, clips, render_master, thumbnails, shorts_derive, qc, outbox.
"""

from __future__ import annotations

from ns_studio.media.captions import CaptionsStage
from ns_studio.media.clips import ClipsStage
from ns_studio.media.derive import ShortsDeriveStage
from ns_studio.media.longform import RenderMasterStage
from ns_studio.media.master import DurationCheckStage, ShortsTimingStage
from ns_studio.media.outbox import OutboxStage
from ns_studio.media.qc import QcStage
from ns_studio.media.render import RenderStage
from ns_studio.media.thumbnails import ThumbnailsStage
from ns_studio.media.voice import VoiceStage
from ns_studio.stages import Stage, register

SHORTS_MEDIA_STAGES: list[Stage] = [
    VoiceStage(), CaptionsStage(), ClipsStage(), RenderStage(), QcStage(), OutboxStage(),
]

_voice, _captions, _clips, _qc, _outbox = (
    SHORTS_MEDIA_STAGES[0], SHORTS_MEDIA_STAGES[1], SHORTS_MEDIA_STAGES[2],
    SHORTS_MEDIA_STAGES[4], SHORTS_MEDIA_STAGES[5],
)
MASTER_MEDIA_STAGES: list[Stage] = [
    _voice, DurationCheckStage(), _captions, ShortsTimingStage(), _clips, RenderMasterStage(),
    ThumbnailsStage(), ShortsDeriveStage(), _qc, _outbox,
]

for _st in {s.name: s for s in [*SHORTS_MEDIA_STAGES, *MASTER_MEDIA_STAGES]}.values():
    register(_st)
