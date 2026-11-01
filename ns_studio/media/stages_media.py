"""Registers the media stages and appends them to the `shorts_only` format (component C6).

Import this module (the CLI does) to make `voice, captions, clips, render, qc, outbox` available.
"""

from __future__ import annotations

from ns_studio.media.captions import CaptionsStage
from ns_studio.media.clips import ClipsStage
from ns_studio.media.outbox import OutboxStage
from ns_studio.media.qc import QcStage
from ns_studio.media.render import RenderStage
from ns_studio.media.voice import VoiceStage
from ns_studio.stages import Stage, register

SHORTS_MEDIA_STAGES: list[Stage] = [
    VoiceStage(), CaptionsStage(), ClipsStage(), RenderStage(), QcStage(), OutboxStage(),
]

for _st in SHORTS_MEDIA_STAGES:
    register(_st)
