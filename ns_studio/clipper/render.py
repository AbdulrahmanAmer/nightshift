"""C8 Clipper: per-template FFmpeg render (spec 19.2 C7).

`build_filter_graph` is pure: it turns a template plus crops into one `-filter_complex` string. The panels
come from the template's format preset through the generic engine in `ns_studio.media.layout` (crop + scale
+ overlay, blurred fill); this module adds the speaker border and the burned `ass=` captions.
`render_clip` adds the two-pass loudnorm to -14 LUFS (behind a peak limiter when the clip is too peaky to be
lifted linearly) and runs FFmpeg through `ns_common.ffmpeg.run`.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from ns_common import ffmpeg
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import CANVAS_H, CANVAS_W, ClipTemplate, Crop, Format, Rect
from ns_studio.clipper import layout
from ns_studio.media.layout import SourceSpec, build_regions_graph, fit_facecam_height
from ns_studio.media.render import escape_filter_path

TARGET_LUFS = -14.0
TARGET_TP = -1.5
TARGET_LRA = 11.0
LINEAR_TOLERANCE_DB = 0.1   # loudnorm limiting a peak by a tenth of a dB moves the loudness by less
CEILING_HEADROOM_DB = 2.0   # ~1 dB of loudness the limiter removes, ~1 dB the resample and AAC add to a peak
REENCODE_OVERSHOOT_DB = 1.0
LIMITER_ATTACK_MS = 2.0
LIMITER_RELEASE_MS = 50.0
MAX_LIMITER_PASSES = 4
FPS = 30


@dataclass(frozen=True)
class RenderSpec:
    """Everything needed to render one clip. `crops` use absolute source seconds."""

    video: Path
    out: Path
    start: float
    end: float
    template: ClipTemplate
    crops: list[Crop]
    src_w: int
    src_h: int
    ass: Path | None = None
    encoder: str = "libx264"
    preset: str = "ultrafast"

    @property
    def duration(self) -> float:
        """Clip length in seconds."""
        return self.end - self.start


# ---------- helpers ----------
def filter_path(p: Path) -> str:
    """Escape a path for use inside a single-quoted FFmpeg filter option (Windows drive colons)."""
    return escape_filter_path(p)[1:-1]  # the quoting is added by the caller; apostrophes handled once, in C6


def relative_crops(crops: list[Crop], start: float, end: float) -> list[Crop]:
    """Crops overlapping [start, end], clipped to it and shifted so the clip starts at 0."""
    out: list[Crop] = []
    for c in crops:
        a, b = max(c.t_start, start), min(c.t_end, end)
        if b - a > 1e-3:
            out.append(c.model_copy(update={"t_start": round(a - start, 3), "t_end": round(b - start, 3)}))
    if not out and crops:
        nearest = min(crops, key=lambda c: min(abs(c.t_start - start), abs(c.t_end - start)))
        out.append(nearest.model_copy(update={"t_start": 0.0, "t_end": round(end - start, 3)}))
    return out


def lerp_expr(points: list[tuple[float, float]]) -> str:
    """FFmpeg expression of `t` interpolating linearly between (t, value) points (flat outside)."""
    pts: list[tuple[float, float]] = []
    for p in sorted(points):
        if pts and abs(p[0] - pts[-1][0]) < 1e-3:
            pts[-1] = p
        else:
            pts.append(p)
    # drop interior points that lie on a flat stretch
    slim = [
        p
        for i, p in enumerate(pts)
        if i in (0, len(pts) - 1) or not (pts[i - 1][1] == p[1] == pts[i + 1][1])
    ]
    if len(slim) == 1 or all(v == slim[0][1] for _, v in slim):
        return f"{slim[0][1]:g}"
    expr = f"{slim[-1][1]:g}"
    for (t0, v0), (t1, v1) in reversed(list(zip(slim, slim[1:], strict=False))):
        seg = f"{v0:g}" if v0 == v1 else f"{v0:g}+({v1:g}-{v0:g})*(t-{t0:.3f})/{t1 - t0:.3f}"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"
    return f"if(lt(t,{slim[0][0]:.3f}),{slim[0][1]:g},{expr})"


def _crop(r_x: int, r_y: int, r_w: int, r_h: int) -> str:
    return f"crop={r_w}:{r_h}:{r_x}:{r_y}"


def _speaker_borders(crops: list[Crop], border_px: int) -> list[str]:
    """drawbox filters outlining the speaking panel (0 top, 1 bottom) in its time intervals."""
    if border_px <= 0:
        return []
    out: list[str] = []
    for idx, y in ((0, 0), (1, CANVAS_H // 2)):
        spans = [(c.t_start, c.t_end) for c in crops if c.speaking == idx]
        if not spans:
            continue
        enable = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in spans)
        out.append(
            f"drawbox=x=0:y={y}:w={CANVAS_W}:h={CANVAS_H // 2}:color=white@0.9:t={border_px}"
            f":enable='{enable}'"
        )
    return out


def _rect(r: Rect) -> tuple[int, int, int, int]:
    return r.x, r.y, r.w, r.h


def region_sources(spec: RenderSpec, crops: list[Crop]) -> dict[str, SourceSpec]:
    """What each region source of the template's format shows, from the planned crops (clip-relative)."""
    first = crops[0]
    w, h = spec.src_w, spec.src_h
    kind = spec.template.layout
    if kind in ("gaming", "screen_cam"):
        assert first.region_b is not None
        cam = SourceSpec(w, h, crop=_rect(first.region_b))
        if kind == "gaming":
            return {"facecam": cam, "full": SourceSpec(w, h, crop=_rect(first.region))}
        return {"facecam": cam, "screen": SourceSpec(w, h, fit="contain"), "full": SourceSpec(w, h)}
    if kind == "stacked":
        assert first.region_b is not None
        return {"face:0": SourceSpec(w, h, crop=_rect(first.region)),
                "face:1": SourceSpec(w, h, crop=_rect(first.region_b)), "full": SourceSpec(w, h)}
    if kind == "tracked":
        pts = [((c.t_start + c.t_end) / 2.0, float(c.region.x)) for c in crops]
        pts = [(0.0, pts[0][1]), *pts, (spec.duration, pts[-1][1])]
        window = SourceSpec(w, h, crop=_rect(first.region), x_expr=lerp_expr(pts))
        return {"face:0": window, "full": window}
    return {"full": SourceSpec(w, h, crop=_rect(first.region))}  # center: the blurred fill ignores the crop


def clip_format(spec: RenderSpec, first: Crop) -> Format:
    """The template's format preset with the facecam panel sized to the camera's aspect (spec 19.3)."""
    fmt = layout.format_for(spec.template.name)
    if first.region_b is not None and any(r.source == "facecam" for r in fmt.layout.regions):
        fmt = fit_facecam_height(fmt, first.region_b.w, first.region_b.h)
    return fmt


# ---------- graph ----------
def build_filter_graph(spec: RenderSpec) -> str:
    """The video part of `-filter_complex`; its output pad is `[vout]`.

    The regions come from the template's format preset and are drawn by the generic engine
    (`ns_studio.media.layout`); this function only supplies the sources, the speaker border and the captions.
    """
    tpl = spec.template
    crops = relative_crops(spec.crops, spec.start, spec.end)
    if not crops:
        raise NsError("NS-E401", "no crops for the clip")
    first = crops[0]
    fmt = clip_format(spec, first)
    parts: list[str] = [f"[0:v]setpts=PTS-STARTPTS,fps={FPS},setsar=1[src]"]
    parts += build_regions_graph(fmt, region_sources(spec, crops), out_label="v0", duration_s=spec.duration)
    tail = ",".join(_speaker_borders(crops, tpl.speaker_border_px)) if tpl.layout == "stacked" else ""
    chain = ["[v0]"]
    if tail:
        chain.append(tail + ",")
    chain.append("format=yuv420p")
    if spec.ass is not None:
        chain.append(f",ass='{filter_path(spec.ass)}'")
    parts.append("".join(chain) + "[vout]")
    return ";".join(parts)


# ---------- audio ----------
_JSON_BLOCK = re.compile(r"\{[^{}]*\}", re.S)


def parse_loudnorm(stderr: str) -> dict[str, float] | None:
    """The measured values of a loudnorm pass-1 report; None when unmeasurable (silence)."""
    blocks = _JSON_BLOCK.findall(stderr)
    if not blocks:
        return None
    try:
        raw = json.loads(blocks[-1])
        keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        vals = {k: float(raw[k]) for k in keys}
    except (ValueError, KeyError):
        return None
    return vals if all(math.isfinite(v) for v in vals.values()) else None


def linear_possible(input_i: float, input_tp: float) -> bool:
    """Whether a plain gain to `TARGET_LUFS` keeps the true peak at or under `TARGET_TP`.

    loudnorm only stays linear when it does; otherwise it limits on the fly and ends short of the target
    (a -20.4 LUFS / -0.5 dBTP clip came out at -17.1 LUFS and failed QC with NS-E404).
    """
    return input_tp + (TARGET_LUFS - input_i) <= TARGET_TP + LINEAR_TOLERANCE_DB


def peak_ceiling_db(input_i: float) -> float:
    """The sample-peak ceiling (dBFS) that lets a clip of `input_i` LUFS reach the target linearly.

    The target allows `TARGET_TP - TARGET_LUFS` dB between loudness and peak; the ceiling keeps
    `CEILING_HEADROOM_DB` of that for the limiter's own loudness loss and for the overshoot that the final
    resample and AAC encode add to a sharp peak.
    """
    return input_i + (TARGET_TP - TARGET_LUFS) - CEILING_HEADROOM_DB


def limiter_filter(ceiling_db: float) -> str:
    """A lookahead `alimiter` holding the sample peaks at `ceiling_db` (auto-level off, no makeup gain)."""
    lin = min(1.0, max(0.0625, 10 ** (ceiling_db / 20)))  # alimiter accepts 0.0625 (-24 dBFS) to 1
    return f"alimiter=limit={lin:.4g}:attack={LIMITER_ATTACK_MS:g}:release={LIMITER_RELEASE_MS:g}:level=0"


def measure_loudness(spec: RenderSpec, log: NsLogger | None = None, pre: str = "") -> dict[str, float] | None:
    """Loudnorm pass 1 over the clip's audio, after the optional `pre` filter (what pass 2 will see)."""
    chain = f"{pre}," if pre else ""
    proc = ffmpeg.run(
        [
            "-ss", f"{spec.start:.3f}", "-t", f"{spec.duration:.3f}", "-i", str(spec.video), "-vn",
            "-af", f"{chain}loudnorm=I={TARGET_LUFS:g}:TP={TARGET_TP:g}:LRA={TARGET_LRA:g}:print_format=json",
            "-f", "null", "-",
        ],
        log=log,
    )
    return parse_loudnorm(proc.stderr)


@dataclass(frozen=True)
class LoudnessPlan:
    """How a clip's audio reaches the target: the peak stage (empty when linear) and the pass-1 values.

    `measured` is what loudnorm will see (after `pre`); `source` is the clip as recorded.
    """

    pre: str
    measured: dict[str, float] | None
    source: dict[str, float] | None
    ceiling_db: float | None = None

    @property
    def mode(self) -> str:
        """`linear` (two-pass loudnorm alone) or `limited` (peaks held down first)."""
        return "limited" if self.pre else "linear"

    def report(self) -> dict[str, float | str | None]:
        """The `loudness` entry of `render/report.json`: enough to explain a QC miss from the job folder."""
        src = self.source
        out: dict[str, float | str | None] = {
            "mode": self.mode,
            "input_i": src["input_i"] if src else None,
            "input_tp": src["input_tp"] if src else None,
        }
        if self.pre and self.measured and self.ceiling_db is not None:
            out["limited_i"] = self.measured["input_i"]
            out["limited_tp"] = self.measured["input_tp"]
            out["ceiling_db"] = round(self.ceiling_db, 2)
        return out


def plan_loudness(spec: RenderSpec, log: NsLogger | None = None) -> LoudnessPlan:
    """Pass 1, and when the target is out of linear reach, a peak limiter measured again until it fits.

    Quiet recordings with high peaks (phone audio, podcasts) sit 20 dB or more under their own peaks, so
    lifting them to -14 LUFS would clip. The limiter goes before loudnorm and pass 1 is repeated on its
    output, so the `measured_*` values are those loudnorm sees and it can stay linear. Each repeat lowers the
    ceiling by the shortfall; after `MAX_LIMITER_PASSES` loudnorm takes over as it did before.
    """
    source = measure_loudness(spec, log)
    if source is None or linear_possible(source["input_i"], source["input_tp"]):
        return LoudnessPlan("", source, source)
    ceiling = peak_ceiling_db(source["input_i"])
    plan = LoudnessPlan("", source, source)
    for attempt in range(MAX_LIMITER_PASSES):
        pre = limiter_filter(ceiling)
        measured = measure_loudness(spec, log, pre)
        if measured is None:
            break
        plan = LoudnessPlan(pre, measured, source, ceiling)
        if log:
            log.info("loudness_limited", attempt=attempt + 1, ceiling_db=round(ceiling, 2),
                     input_i=measured["input_i"], input_tp=measured["input_tp"])
        peak = measured["input_tp"] + REENCODE_OVERSHOOT_DB
        if linear_possible(measured["input_i"], peak):
            break
        ceiling -= peak + (TARGET_LUFS - measured["input_i"]) - TARGET_TP + 0.5
    return plan


def audio_filter(measured: dict[str, float] | None, pre: str = "") -> str:
    """The audio chain for `[aout]`: the `pre` peak stage, then two-pass loudnorm (one pass if unmeasured)."""
    base = f"loudnorm=I={TARGET_LUFS:g}:TP={TARGET_TP:g}:LRA={TARGET_LRA:g}"
    if measured:
        base += (
            f":measured_I={measured['input_i']:.2f}:measured_TP={measured['input_tp']:.2f}"
            f":measured_LRA={measured['input_lra']:.2f}:measured_thresh={measured['input_thresh']:.2f}"
            f":offset={measured['target_offset']:.2f}:linear=true"
        )
    return f"{pre + ',' if pre else ''}{base},aresample=48000"


def encoder_args(encoder: str, preset: str) -> list[str]:
    """Video encoder options for `libx264` or `h264_nvenc`."""
    cap = ["-maxrate", "12M", "-bufsize", "24M"]          # see ns_studio.media.render.MAXRATE
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "vbr", "-cq", "23", "-b:v", "0", *cap]
    if preset == "ultrafast":
        preset = "veryfast"
    return ["-c:v", "libx264", "-preset", preset, "-crf", "22", *cap]


def build_args(spec: RenderSpec, measured: dict[str, float] | None, pre: str = "") -> list[str]:
    """Full FFmpeg argument list for the clip."""
    graph = build_filter_graph(spec) + f";[0:a]{audio_filter(measured, pre)}[aout]"
    return [
        "-ss", f"{spec.start:.3f}", "-t", f"{spec.duration:.3f}", "-i", str(spec.video),
        "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
        *encoder_args(spec.encoder, spec.preset),
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(spec.out),
    ]


def render_clip_report(
    spec: RenderSpec, log: NsLogger | None = None
) -> tuple[Path, dict[str, float | str | None]]:
    """Render one clip to `spec.out`; returns the path and the clip's `loudness` report entry.

    Falls back from NVENC to libx264 (NS-W401) when the encoder fails.
    """
    spec.out.parent.mkdir(parents=True, exist_ok=True)
    plan = plan_loudness(spec, log)
    try:
        ffmpeg.run(build_args(spec, plan.measured, plan.pre), log=log)
    except NsError:
        if spec.encoder != "h264_nvenc":
            raise
        if log:
            log.warn("nvenc_fallback", "NVENC failed, retrying with libx264", code="NS-W401")
        fallback = RenderSpec(**{**spec.__dict__, "encoder": "libx264", "preset": "veryfast"})
        ffmpeg.run(build_args(fallback, plan.measured, plan.pre), log=log)
    return spec.out, plan.report()


def render_clip(spec: RenderSpec, log: NsLogger | None = None) -> Path:
    """Render one clip to `spec.out` (see `render_clip_report`)."""
    return render_clip_report(spec, log)[0]
