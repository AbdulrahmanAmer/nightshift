"""C8 Clipper: per-template FFmpeg render (spec 19.2 C7).

`build_filter_graph` is pure: it turns a template plus crops into one `-filter_complex` string (vstack,
crop + scale, blurred fill, camera overlay, speaker border, burned `ass=` captions). `render_clip` adds
the two-pass loudnorm to -14 LUFS and runs FFmpeg through `ns_common.ffmpeg.run`.
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
from ns_studio.clipper.layout import canvas_panels
from ns_studio.clipper.models import CANVAS_H, CANVAS_W, ClipTemplate, Crop

TARGET_LUFS = -14.0
TARGET_TP = -1.5
TARGET_LRA = 11.0
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
    s = str(p).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    return s


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
            f"drawbox=x=0:y={y}:w={CANVAS_W}:h={CANVAS_H // 2}:color=white@0.9:t={border_px}:enable='{enable}'"
        )
    return out


# ---------- graph ----------
def build_filter_graph(spec: RenderSpec) -> str:
    """The video part of `-filter_complex`; its output pad is `[vout]`."""
    tpl = spec.template
    crops = relative_crops(spec.crops, spec.start, spec.end)
    if not crops:
        raise NsError("NS-E401", "no crops for the clip")
    first = crops[0]
    parts: list[str] = [f"[0:v]setpts=PTS-STARTPTS,fps={FPS},setsar=1[src]"]
    panels = canvas_panels(tpl, first)
    layout = tpl.layout
    tail = ""
    if layout in ("gaming", "screen_cam"):
        assert first.region_b is not None
        main_id = "game" if layout == "gaming" else "screen"
        cam_p, main_p = panels["cam"], panels[main_id]
        cb = first.region_b
        parts.append("[src]split=2[s1][s2]")
        parts.append(f"[s1]{_crop(cb.x, cb.y, cb.w, cb.h)},scale={cam_p.w}:{cam_p.h}:flags=lanczos[cam]")
        if layout == "gaming":
            m = first.region
            parts.append(f"[s2]{_crop(m.x, m.y, m.w, m.h)},scale={main_p.w}:{main_p.h}:flags=lanczos[main]")
        else:
            parts.append(
                f"[s2]scale={main_p.w}:{main_p.h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={main_p.w}:{main_p.h}:(ow-iw)/2:(oh-ih)/2:color=black[main]"
            )
        parts.append("[cam][main]vstack=inputs=2[v0]")
    elif layout == "stacked":
        assert first.region_b is not None
        top_p, bot_p = panels["top"], panels["bottom"]
        a, b = first.region, first.region_b
        parts.append("[src]split=2[s1][s2]")
        parts.append(f"[s1]{_crop(a.x, a.y, a.w, a.h)},scale={top_p.w}:{top_p.h}:flags=lanczos[top]")
        parts.append(f"[s2]{_crop(b.x, b.y, b.w, b.h)},scale={bot_p.w}:{bot_p.h}:flags=lanczos[bot]")
        parts.append("[top][bot]vstack=inputs=2[v0]")
        tail = ",".join(_speaker_borders(crops, tpl.speaker_border_px))
    elif layout == "tracked":
        main_p = panels["main"]
        cw, ch = first.region.w, first.region.h
        pts = [((c.t_start + c.t_end) / 2.0, float(c.region.x)) for c in crops]
        pts = [(0.0, pts[0][1]), *pts, (spec.duration, pts[-1][1])]
        x_expr = lerp_expr(pts)
        parts.append(
            f"[src]crop=w={cw}:h={ch}:x='clip({x_expr},0,{spec.src_w - cw})':y={first.region.y}:exact=1,"
            f"scale={main_p.w}:{main_p.h}:flags=lanczos[v0]"
        )
    else:  # center
        bg_p, fg_p = panels["bg"], panels["fg"]
        m = first.region
        parts.append("[src]split=2[s1][s2]")
        parts.append(
            f"[s1]scale={bg_p.w // 4}:{bg_p.h // 4}:force_original_aspect_ratio=increase,"
            f"crop={bg_p.w // 4}:{bg_p.h // 4},boxblur=10:2,eq=brightness=-0.2,"
            f"scale={bg_p.w}:{bg_p.h}[bg]"
        )
        parts.append(f"[s2]{_crop(m.x, m.y, m.w, m.h)},scale={fg_p.w}:{fg_p.h}:flags=lanczos[fg]")
        parts.append(f"[bg][fg]overlay={fg_p.x}:{fg_p.y}[v0]")
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
        vals = {k: float(raw[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (ValueError, KeyError):
        return None
    return vals if all(math.isfinite(v) for v in vals.values()) else None


def measure_loudness(spec: RenderSpec, log: NsLogger | None = None) -> dict[str, float] | None:
    """Loudnorm pass 1 over the clip's audio."""
    proc = ffmpeg.run(
        [
            "-ss", f"{spec.start:.3f}", "-t", f"{spec.duration:.3f}", "-i", str(spec.video), "-vn",
            "-af", f"loudnorm=I={TARGET_LUFS:g}:TP={TARGET_TP:g}:LRA={TARGET_LRA:g}:print_format=json",
            "-f", "null", "-",
        ],
        log=log,
    )
    return parse_loudnorm(proc.stderr)


def audio_filter(measured: dict[str, float] | None) -> str:
    """The audio chain for `[aout]`: two-pass loudnorm when measured, single-pass otherwise."""
    base = f"loudnorm=I={TARGET_LUFS:g}:TP={TARGET_TP:g}:LRA={TARGET_LRA:g}"
    if measured:
        base += (
            f":measured_I={measured['input_i']:.2f}:measured_TP={measured['input_tp']:.2f}"
            f":measured_LRA={measured['input_lra']:.2f}:measured_thresh={measured['input_thresh']:.2f}"
            f":offset={measured['target_offset']:.2f}:linear=true"
        )
    return f"{base},aresample=48000"


def encoder_args(encoder: str, preset: str) -> list[str]:
    """Video encoder options for `libx264` or `h264_nvenc`."""
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", preset, "-crf", "20"]


def build_args(spec: RenderSpec, measured: dict[str, float] | None) -> list[str]:
    """Full FFmpeg argument list for the clip."""
    graph = build_filter_graph(spec) + f";[0:a]{audio_filter(measured)}[aout]"
    return [
        "-ss", f"{spec.start:.3f}", "-t", f"{spec.duration:.3f}", "-i", str(spec.video),
        "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
        *encoder_args(spec.encoder, spec.preset),
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(spec.out),
    ]


def render_clip(spec: RenderSpec, log: NsLogger | None = None) -> Path:
    """Render one clip to `spec.out`; falls back from NVENC to libx264 (NS-W401) when the encoder fails."""
    spec.out.parent.mkdir(parents=True, exist_ok=True)
    measured = measure_loudness(spec, log)
    try:
        ffmpeg.run(build_args(spec, measured), log=log)
    except NsError:
        if spec.encoder != "h264_nvenc":
            raise
        if log:
            log.warn("nvenc_fallback", "NVENC failed, retrying with libx264", code="NS-W401")
        fallback = RenderSpec(**{**spec.__dict__, "encoder": "libx264", "preset": "veryfast"})
        ffmpeg.run(build_args(fallback, measured), log=log)
    return spec.out
