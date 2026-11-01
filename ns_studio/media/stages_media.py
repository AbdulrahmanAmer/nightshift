"""Registers the media stages and appends them to the `shorts_only` and `master_first` formats (C6).

Import this module (the CLI does) to make the stages available. Shared names (`voice`, `captions`, `clips`,
`qc`, `outbox`) are single stage objects that dispatch on `ctx.fmt`.

`master_first` receives the media stages after C5's text stages, in the order
`voice, duration_check, captions, shorts_timing, clips, render_master, thumbnails, shorts_derive, qc, outbox`.
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
from ns_studio.orchestrator import FORMAT_STAGES
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
for _st in SHORTS_MEDIA_STAGES:
    if _st.name not in FORMAT_STAGES["shorts_only"]:
        FORMAT_STAGES["shorts_only"].append(_st.name)
for _st in MASTER_MEDIA_STAGES:
    if _st.name not in FORMAT_STAGES["master_first"]:
        FORMAT_STAGES["master_first"].append(_st.name)
