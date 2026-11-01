"""C8 Clipper: job parameters and metadata models; the shared clipper schemas come from `ns_common.models`.

`transcript`, `clip_plan`, `clip_layout` and `clip_template` moved to `ns_common/models.py` (CR-C8-1, applied
in e0c34f5). They are re-exported here for one release (deprecated; import them from `ns_common.models`) and
`ClipItem` stays as an alias of `ClipPlanItem`.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import Field

from ns_common.errors import NsError
from ns_common.models import (
    CANVAS_H,
    CANVAS_W,
    JOB_ID_RE,
    SLUG_RE,
    ClipLayout,
    ClipPlanItem,
    ClipRef,
    ClipTemplate,
    Crop,
    FaceFrame,
    Rect,
    Region,
    Segment,
    Strict,
    Transcript,
    Word,
)

__all__ = [
    "CANVAS_H",
    "CANVAS_W",
    "TEMPLATE_NAMES",
    "Analysis",
    "ClipItem",
    "ClipLayout",
    "ClipMetadata",
    "ClipParams",
    "ClipTemplate",
    "Crop",
    "FaceFrame",
    "Loudness",
    "LoudPeak",
    "Rect",
    "Region",
    "Segment",
    "SilenceSpan",
    "Transcript",
    "Word",
]

TEMPLATE_NAMES: tuple[str, ...] = (
    "gaming_facecam",
    "podcast_stacked",
    "podcast_single",
    "talking_head",
    "center_crop",
    "screen_plus_cam",
)

# Deprecated alias (CR-C8-1): the clip plan entry is `ns_common.models.ClipPlanItem`.
ClipItem = ClipPlanItem


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
