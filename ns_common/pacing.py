"""Pacing options: words per minute and the TTS speed that makes the voice speak at that pace (spec 7.0)."""

from __future__ import annotations

#: Speaking rate of each `pacing` option (spec section 17).
PACING_WPM: dict[str, int] = {"brisk": 175, "standard": 155, "measured": 140}
#: The rate a voice speaks at `Voice.speed == 1.0` (the budget's "standard" pace).
NOMINAL_WPM = 155
SPEED_MIN, SPEED_MAX = 0.7, 1.3  # the range `Voice.speed` and the TTS API accept


def pacing_speed(voice_speed: float, pacing: str) -> float:
    """TTS speed for a job: the channel voice's own speed scaled by the pacing's rate, clamped to 0.7-1.3.

    `brisk` (175 wpm) speaks 13 % faster than `standard`, `measured` (140 wpm) 10 % slower, so the seconds a
    word budget produces match the seconds QC expects. Raises KeyError for an unknown pacing.
    """
    speed = voice_speed * PACING_WPM[pacing] / NOMINAL_WPM
    return round(min(SPEED_MAX, max(SPEED_MIN, speed)), 3)
