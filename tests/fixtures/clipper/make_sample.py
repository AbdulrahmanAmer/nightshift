"""Synthetic Clipper fixture generator (C8).

`make_sample(out_dir)` writes three files:
  sample.mp4              90 s, 1280x720 testsrc2 background, a 320x180 "facecam" at the top right holding a
                          moving filled ellipse inside a yellow frame, and a 440 Hz tone with silences at the
                          end of every 15 s block
  sample.transcript.json  a fake transcript (6 segments of 15 s, words timed across the 90 s)
  sample.truth.json       ground truth for tests: frame size, duration, facecam rectangle in source pixels
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ns_common import ffmpeg

WIDTH = 1280
HEIGHT = 720
DURATION_S = 90
FPS = 30
CAM_W = 320
CAM_H = 180
CAM_X = WIDTH - CAM_W - 16
CAM_Y = 16
SEG_S = 15.0
SPEECH_LEAD_S = 0.5
SPEECH_TAIL_S = 0.7

# Six segments of three sentences each (spoken at about 2.5 words per second).
SENTENCES: list[list[str]] = [
    [
        "Welcome back everyone, today we are going to test the brand new route through the mountain level.",
        "Every checkpoint here saves your progress automatically, so nothing is ever lost.",
        "Let us jump in right now and see how far we can get.",
    ],
    [
        "The first trick is to hug the left wall and wait patiently for the guard to turn.",
        "Most players miss the narrow ledge because the camera angle hides it completely.",
        "Once you land on it, the whole door sequence opens up for you.",
    ],
    [
        "Why does this work at all, you might ask?",
        "The ledge collision box is bigger than its visible model, so the game forgives you.",
        "That single detail alone saves about forty seconds on every single run you make.",
    ],
    [
        "Now the second section is where most people lose their patience.",
        "Take your time, count three full beats, and then dash across the gap.",
        "If you rush it, the platform drops and you have to start over.",
    ],
    [
        "Here is my favourite part of the whole level.",
        "The boss only attacks when you stand still, so keep moving in wide circles.",
        "After four laps the shield breaks and the finishing move becomes available to you.",
    ],
    [
        "That is the full route from the very start to the finish.",
        "If this helped you, share it with a friend who is still stuck.",
        "Thanks for watching, and I will see you in the next one.",
    ],
]


@dataclass(frozen=True)
class SamplePaths:
    """Locations of the generated fixture files."""

    video: Path
    transcript: Path
    truth: Path


def facecam_truth() -> dict[str, int]:
    """The drawn facecam rectangle in source pixels."""
    return {"x": CAM_X, "y": CAM_Y, "w": CAM_W, "h": CAM_H}


def build_transcript() -> dict:
    """Fake transcript for the 90 s sample: 6 segments, words spread across each segment."""
    segments: list[dict] = []
    words: list[dict] = []
    for i, sentences in enumerate(SENTENCES):
        seg_start = i * SEG_S
        speech_start = seg_start + SPEECH_LEAD_S
        speech_end = seg_start + SEG_S - SPEECH_TAIL_S
        tokens = [t for s in sentences for t in s.split()]
        slot = (speech_end - speech_start) / len(tokens)
        for j, tok in enumerate(tokens):
            w_start = speech_start + j * slot
            words.append(
                {"w": tok, "start": round(w_start, 3), "end": round(w_start + 0.85 * slot, 3), "seg": i}
            )
        segments.append(
            {
                "i": i,
                "start": round(speech_start, 3),
                "end": round(words[-1]["end"], 3),
                "text": " ".join(sentences),
            }
        )
    return {
        "source": "sample.mp4",
        "duration_s": float(DURATION_S),
        "language": "en",
        "segments": segments,
        "words": words,
    }


def _make_video(out: Path) -> None:
    ellipse = (
        "geq=lum='if(lt(hypot((X-(160+90*sin(T*1.5)))/40,(Y-(90+30*cos(T*2)))/50),1),235,60)'"
        ":cb=128:cr=128"
    )
    cam = (
        f"color=c=0x303030:s={CAM_W}x{CAM_H}:r={FPS}:d={DURATION_S},{ellipse},"
        f"drawbox=x=0:y=0:w={CAM_W}:h={CAM_H}:color=yellow:t=6"
    )
    tone = (
        f"sine=frequency=440:sample_rate=48000:duration={DURATION_S},volume=4,"
        f"volume=0:enable='gte(mod(t,{SEG_S:g}),14.4)'"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(
        [
            "-f", "lavfi", "-i", f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={DURATION_S}",
            "-f", "lavfi", "-i", cam,
            "-f", "lavfi", "-i", tone,
            "-filter_complex", f"[0:v][1:v]overlay={CAM_X}:{CAM_Y}[v]",
            "-map", "[v]", "-map", "2:a",
            "-t", str(DURATION_S),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", str(FPS),
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "1",
            "-movflags", "+faststart", str(out),
        ]
    )


def make_sample(out_dir: Path, *, with_video: bool = True) -> SamplePaths:
    """Write the fixture files into `out_dir`; `with_video=False` skips FFmpeg (JSON files only)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    video = out_dir / "sample.mp4"
    transcript = out_dir / "sample.transcript.json"
    truth = out_dir / "sample.truth.json"
    transcript.write_text(json.dumps(build_transcript(), indent=1) + "\n", encoding="utf-8")
    truth.write_text(
        json.dumps(
            {"width": WIDTH, "height": HEIGHT, "duration_s": DURATION_S, "facecam": facecam_truth()},
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    if with_video:
        _make_video(video)
    return SamplePaths(video=video, transcript=transcript, truth=truth)


if __name__ == "__main__":  # pragma: no cover - manual use
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sample_out")
    make_sample(target)
