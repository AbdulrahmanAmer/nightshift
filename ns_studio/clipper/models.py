"""C8 Clipper: local pydantic models for the clipper schemas of spec section 15.6.

`transcript`, `clip_plan`, `clip_layout` and `clip_template` live here until CR-C8-1 moves them to
`ns_common/models.py`. All models forbid unknown fields, like the shared ones.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator

from ns_common.errors import NsError
from ns_common.models import JOB_ID_RE, SLUG_RE, ClipRef, Strict

TEMPLATE_NAMES: tuple[str, ...] = (
    "gaming_facecam",
    "podcast_stacked",
    "podcast_single",
    "talking_head",
    "center_crop",
    "screen_plus_cam",
)
CANVAS_W = 1080
CANVAS_H = 1920


# ---------- geometry ----------
class Rect(Strict):
    """Axis-aligned rectangle in pixels of the frame it refers to."""

    x: int
    y: int
    w: int = Field(gt=0)
    h: int = Field(gt=0)

    @property
    def right(self) -> int:
        """Right edge (exclusive)."""
        return self.x + self.w

    @property
    def bottom(self) -> int:
        """Bottom edge (exclusive)."""
        return self.y + self.h

    @property
    def area(self) -> int:
        """Area in square pixels."""
        return self.w * self.h

    @property
    def center(self) -> tuple[float, float]:
        """Centre point (x, y)."""
        return self.x + self.w / 2.0, self.y + self.h / 2.0

    def inside(self, outer: Rect, tol: int = 0) -> bool:
        """True when this rectangle lies within `outer` (with `tol` pixels of slack)."""
        return (
            self.x >= outer.x - tol
            and self.y >= outer.y - tol
            and self.right <= outer.right + tol
            and self.bottom <= outer.bottom + tol
        )


# ---------- transcript ----------
class Segment(Strict):
    """One transcript segment."""

    i: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    speaker: int | None = None


class Word(Strict):
    """One timed word; `w` keeps the transcript's spelling and punctuation."""

    w: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    seg: int = Field(ge=0)


class Transcript(Strict):
    """Spec 15.6 `transcript`."""

    source: str
    duration_s: float = Field(gt=0)
    language: str = "en"
    segments: list[Segment]
    words: list[Word]

    @model_validator(mode="after")
    def _ordered(self) -> Transcript:
        for s in self.segments:
            if s.end < s.start:
                raise ValueError(f"segment {s.i} ends before it starts")
        for w in self.words:
            if w.end < w.start:
                raise ValueError(f"word {w.w!r} ends before it starts")
        return self


# ---------- clip plan ----------
class ClipItem(Strict):
    """One entry of spec 15.6 `clip_plan`."""

    k: int = Field(ge=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    title: str = Field(max_length=100)
    hook_line: str | None = None
    why: str = ""
    score: float = Field(ge=0.0, le=1.0)
    speakers: list[int] | None = None
    keywords: list[str] = []
    layout: str | None = None  # format preset chosen by the model (CR-C8-8), final one is in clip_layout
    layout_reason: str | None = None
    status: Literal["kept", "dropped"] = "kept"
    reasons: list[str] = []  # why a candidate was dropped (judge, filters)

    @model_validator(mode="after")
    def _ordered(self) -> ClipItem:
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be greater than start_s")
        return self

    @property
    def duration_s(self) -> float:
        """Clip length in seconds."""
        return self.end_s - self.start_s


# ---------- clip layout ----------
class FaceFrame(Strict):
    """Face boxes seen at time `t`; `speaking` indexes `boxes` (sorted left to right)."""

    t: float = Field(ge=0)
    boxes: list[Rect]
    speaking: int | None = None


class Crop(Strict):
    """Source region(s) shown during [t_start, t_end] (source seconds).

    `region` is the main panel's source rectangle; `region_b` the second panel's (camera for
    gaming_facecam / screen_plus_cam, lower speaker for podcast_stacked); `speaking` the index
    (0 top, 1 bottom) of the highlighted speaker for podcast_stacked.
    """

    t_start: float = Field(ge=0)
    t_end: float = Field(ge=0)
    region: Rect
    region_b: Rect | None = None
    speaking: int | None = None


class ClipLayout(Strict):
    """Spec 15.6 `clip_layout`; `note` records fallbacks such as NS-W402."""

    template: str
    facecam: Rect | None = None
    faces: list[FaceFrame] = []
    crops: list[Crop] = []
    note: str | None = None
    per_clip: dict[str, str] = {}  # clip k -> template used for that clip
    notes: dict[str, str] = {}  # clip k -> NS-W402 fallback text


# ---------- clip template (presets/clip_templates.yaml) ----------
class Region(Strict):
    """A panel of the 1080x1920 canvas. `y="seam"` and `h="auto"|"rest"` are resolved from the camera."""

    id: str
    x: int = 0
    y: int | Literal["seam"] = 0
    w: int = CANVAS_W
    h: int | Literal["auto", "rest"] = "rest"
    max_h: int | None = None


class ClipTemplate(Strict):
    """One entry of clip_templates.yaml."""

    name: str
    layout: Literal["gaming", "stacked", "tracked", "center", "screen_cam"]
    description: str
    needs: Literal["none", "facecam", "faces_1", "faces_2"] = "none"
    when: str = ""
    regions: list[Region]
    caption_anchor_y_pct: float | Literal["seam"]
    safe_top: int = 220
    safe_bottom: int = 320
    title_card: bool = False
    title_card_s: float = 1.2
    speaker_border_px: int = 0
    track_deadband: float = 0.2
    track_ramp_s: float = 1.0


# ---------- job parameters ----------
@dataclass
class ClipParams:
    """Clip job parameters, stored at `<job_dir>/clip_params.json`."""

    source: str
    channel: str
    template: str = "auto"
    max_clips: int = 6
    min_s: float = 20.0
    max_s: float = 60.0
    spacing_s: float = 60.0
    rights_confirmed: bool = False
    video_kind: str = "unknown"
    source_id: str | None = None
    source_title: str | None = None

    FILE: ClassVar[str] = "clip_params.json"

    def validate(self) -> None:
        """Raise ValueError for impossible parameter combinations."""
        if self.template != "auto" and self.template not in TEMPLATE_NAMES:
            raise ValueError(f"unknown template {self.template!r}")
        if not 1 <= self.max_clips <= 20:
            raise ValueError("max_clips must be 1..20")
        if not 5 <= self.min_s < self.max_s <= 179:
            raise ValueError("need 5 <= min_s < max_s <= 179")
        if self.spacing_s < 0:
            raise ValueError("spacing_s must be >= 0")

    def save(self, job_dir: Path) -> Path:
        """Write `clip_params.json`."""
        self.validate()
        p = job_dir / self.FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        return p

    @classmethod
    def load(cls, job_dir: Path) -> ClipParams:
        """Read `clip_params.json`; raises NS-E405 when the job has none."""
        p = job_dir / cls.FILE
        if not p.exists():
            raise NsError("NS-E405", f"{cls.FILE} missing in {job_dir}")
        raw: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return cls(**raw)


# ---------- upload sheet ----------
class ClipMetadata(Strict):
    """Upload sheet of a clip (spec 15.6 `metadata` with kind "clip"); see CR-C8-4."""

    channel: str = Field(pattern=SLUG_RE)
    job_id: str = Field(pattern=JOB_ID_RE)
    kind: Literal["clip"] = "clip"
    k: int
    title: str = Field(max_length=100)
    description: str
    tags: list[str]
    hashtags: list[str]
    publish_after: str
    source_video: str
    template: str
    ai_disclosure: Literal["yes", "no"] = "no"
    made_for_kids: bool = False
    age_restrict_suggested: bool = False
    clips: list[ClipRef] = []
    combo_id: str = ""
    prompt_versions: dict[str, int] = {}


# ---------- analysis.json (spec 23.5 step 1) ----------
class SilenceSpan(Strict):
    """A stretch of silence found by `silencedetect`."""

    start: float = Field(ge=0)
    end: float = Field(ge=0)


class LoudPeak(Strict):
    """A loudness peak: second `t` and its level in dBFS."""

    t: float = Field(ge=0)
    db: float


class Loudness(Strict):
    """Integrated loudness / range from `ebur128` and the loudest seconds."""

    integrated_lufs: float | None = None
    lra: float | None = None
    peaks: list[LoudPeak] = []


class Analysis(Strict):
    """`analysis.json`: what FFmpeg and the face detector saw in the whole source video."""

    width: int
    height: int
    aspect: float
    duration_s: float
    fps: float
    scene_cuts: list[float] = []
    scene_cuts_per_min: float = 0.0
    silences: list[SilenceSpan] = []
    loudness: Loudness = Loudness()
    facecam: Rect | None = None
    faces: list[FaceFrame] = []  # one entry per sampled second (1 fps), stable faces only
    n_stable_faces: int = 0
    faces_detected: bool = False  # False when OpenCV or the YuNet model was missing
