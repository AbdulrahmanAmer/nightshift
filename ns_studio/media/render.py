"""FFmpeg filter-graph builders and the render stage (component C6).

One filter graph per video: per-shot trim, scale/crop (or blurred fill), zoompan, concat, colour, vignette,
grain and burned-in ASS captions. Shorts are 1080x1920, masters 1920x1080, 30 fps.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import VideoCfg
from ns_studio.context import JobContext
from ns_studio.media.audio import FINAL_LUFS, FINAL_TP, assemble_audio, loudnorm
from ns_studio.media.caption_plan import AssOptions
from ns_studio.media.captions import Box, caption_band, ensure_contrast, sample_luma
from ns_studio.media.diskfull import run_ffmpeg
from ns_studio.media.presets import (
    FONTS_DIR,
    CaptionStyle,
    VisualTreatment,
    ZoomSpec,
    caption_style,
    visual_treatment,
)
from ns_studio.media.shotplan import Shot, frame_edges
from ns_studio.media.transcribe import Word
from ns_studio.stages import Tenant

VERTICAL = (1080, 1920)
LANDSCAPE = (1920, 1080)
X264_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow",
}


# An apostrophe inside a single-quoted filtergraph value is written as: close the quote, three backslashes
# and a quote (the graph parser leaves one backslash-quote for the option parser), reopen the quote.
_QUOTE_IN_QUOTES = chr(39) + chr(92) * 3 + chr(39) + chr(39)


def escape_filter_path(path: Path) -> str:
    """A path as a single-quoted filtergraph option value: forward slashes, `:` and `'` escaped.

    An apostrophe used to be deleted, which pointed FFmpeg at a folder that does not exist (a home under
    `C:/Users/O'Brien` failed every render).
    """
    p = path.as_posix().replace(":", "\\:").replace("'", _QUOTE_IN_QUOTES)
    return f"'{p}'"


def zoom_filter(z: ZoomSpec, w: int, h: int, fps: int, index: int) -> str | None:
    """The zoompan filter for one shot (None for static shots)."""
    base = f"d=1:s={w}x{h}:fps={fps}"
    centre_x = "x='iw/2-(iw/zoom/2)'"
    centre_y = "y='ih/2-(ih/zoom/2)'"
    if z.mode == "in":
        return f"zoompan=z='min(zoom+{z.rate},{z.max})':{centre_x}:{centre_y}:{base}"
    if z.mode == "out":
        return f"zoompan=z='max({z.max}-on*{z.rate},1)':{centre_x}:{centre_y}:{base}"
    if z.mode == "handheld":
        phase = round(index * 1.7, 1)
        return (
            f"zoompan=z={z.base}:x='iw/2-(iw/zoom/2)+sin(on/9+{phase})*{z.amount}'"
            f":y='ih/2-(ih/zoom/2)+cos(on/11+{phase})*{z.amount}':{base}"
        )
    return None


def shot_chain(i: int, shot: Shot, w: int, h: int, fps: int, frames: int, z: ZoomSpec) -> str:
    """Filter chain turning input `i` into `[v<i>]`: exactly `frames` frames of w x h video.

    Wide clips in a vertical edit are centre-cropped to 4:5 over a blurred fill of themselves (spec §7.4);
    everything else is scaled to cover and cropped.
    """
    src = f"[{i}:v]fps={fps},setpts=PTS-STARTPTS"
    tail = f"tpad=stop_mode=clone:stop_duration=2,trim=end_frame={frames},setpts=PTS-STARTPTS"
    zoom = zoom_filter(z, w, h, fps, i)
    finish = (f",{zoom}" if zoom else "") + f",setsar=1,fps={fps}"
    wide = shot.width / max(1, shot.height) >= 1.2
    if h > w and wide:
        fg_h = round(w * 5 / 4 / 2) * 2
        sw, sh = max(2, w // 10), max(2, h // 10)
        return (
            f"{src},split=2[f{i}][b{i}];"
            f"[b{i}]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},boxblur=3:2,"
            f"scale={w}:{h}[bb{i}];"
            f"[f{i}]crop=w=ih*4/5:h=ih,scale={w}:{fg_h}[ff{i}];"
            f"[bb{i}][ff{i}]overlay=(W-w)/2:(H-h)/2,{tail}{finish}[v{i}]"
        )
    return f"{src},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},{tail}{finish}[v{i}]"


def post_chain(t: VisualTreatment, ass_path: Path | None, fonts_dir: Path | None) -> str:
    """Colour, vignette, grain, captions and pixel format applied to the concatenated video."""
    parts: list[str] = []
    if t.eq:
        parts.append("eq=" + ":".join(f"{k}={v}" for k, v in t.eq.items()))
    if t.curves:
        parts.append(f"curves=preset={t.curves}")
    if t.colorbalance:
        parts.append("colorbalance=" + ":".join(f"{k}={v}" for k, v in t.colorbalance.items()))
    if t.vignette:
        parts.append(f"vignette=angle=PI/{t.vignette:g}")
    if t.grain:
        parts.append(f"noise=alls={t.grain}:allf=t+u")
    if ass_path is not None:
        sub = f"ass=filename={escape_filter_path(ass_path)}"
        if fonts_dir is not None and fonts_dir.is_dir():
            sub += f":fontsdir={escape_filter_path(fonts_dir)}"
        parts.append(sub)
    parts.append("format=yuv420p")
    return ",".join(parts)


def build_graph(
    shots: Sequence[Shot],
    edges: Sequence[int],
    size: tuple[int, int],
    fps: int,
    treatment: VisualTreatment,
    ass_path: Path | None,
    fonts_dir: Path | None = None,
) -> str:
    """The complete filter_complex ending in `[vout]`; input k is the clip of shot k."""
    w, h = size
    chains = [
        shot_chain(i, s, w, h, fps, edges[i + 1] - edges[i], treatment.zoom) for i, s in enumerate(shots)
    ]
    joined = "".join(f"[v{i}]" for i in range(len(shots)))
    chains.append(f"{joined}concat=n={len(shots)}:v=1:a=0[cat]")
    chains.append(f"[cat]{post_chain(treatment, ass_path, fonts_dir)}[vout]")
    return ";".join(chains)


def build_vertical_graph(
    shots: Sequence[Shot], edges: Sequence[int], fps: int, treatment: VisualTreatment,
    ass_path: Path | None, fonts_dir: Path | None = None,
) -> str:
    """Filter graph for a 1080x1920 Short."""
    return build_graph(shots, edges, VERTICAL, fps, treatment, ass_path, fonts_dir)


def build_landscape_graph(
    shots: Sequence[Shot], edges: Sequence[int], fps: int, treatment: VisualTreatment,
    ass_path: Path | None, fonts_dir: Path | None = None,
) -> str:
    """Filter graph for a 1920x1080 master segment."""
    return build_graph(shots, edges, LANDSCAPE, fps, treatment, ass_path, fonts_dir)


#: Bitrate ceiling for a 1080p render (YouTube's own recommendation for 1080p SDR is 8 to 12 Mbit/s; the
#: film-grain `noise` filter in the treatments drives quality-only modes far above that: the first dev-mode
#: Short came out at 134 Mbit/s, 900 MB for 54 s, with `libx264 -preset ultrafast -crf 21`).
MAXRATE = "12M"
BUFSIZE = "24M"


def encoder_args(video: VideoCfg, *, force_cpu: bool = False) -> list[str]:
    """Video encoder arguments from `runtime.video` (NVENC on GPU profiles, libx264 otherwise), with a cap."""
    cap = ["-maxrate", MAXRATE, "-bufsize", BUFSIZE]
    if video.encoder == "h264_nvenc" and not force_cpu:
        return ["-c:v", "h264_nvenc", "-preset", video.preset, "-rc", "vbr", "-cq", "23", "-b:v", "0", *cap]
    # ultrafast turns off nearly every compression tool (huge files); veryfast is still real time on a CPU
    preset = video.preset if video.preset in X264_PRESETS and video.preset != "ultrafast" else "veryfast"
    return ["-c:v", "libx264", "-preset", preset, "-crf", "23", *cap]


def render_video(
    *,
    shots: Sequence[Shot],
    audio: Path,
    out: Path,
    size: tuple[int, int],
    treatment: VisualTreatment,
    video: VideoCfg,
    ass_path: Path | None,
    fonts_dir: Path | None = None,
    log: NsLogger | None = None,
) -> Path:
    """Render `shots` + `audio` into `out` (H.264/AAC, faststart); NS-E401 on failure.

    When NVENC fails the render is retried once with libx264 and NS-W401 is logged.
    """
    if not shots:
        raise NsError("NS-E401", "render needs at least one shot")
    edges = frame_edges([s.end for s in shots], video.fps)
    graph = build_graph(shots, edges, size, video.fps, treatment, ass_path, fonts_dir)
    inputs: list[str] = []
    for i, s in enumerate(shots):
        need = (edges[i + 1] - edges[i]) / video.fps + 0.5
        inputs += ["-ss", f"{s.ss:.2f}", "-t", f"{need:.2f}", "-i", s.path]
    audio_index = len(shots)
    out.parent.mkdir(parents=True, exist_ok=True)

    def attempt(force_cpu: bool) -> None:
        run_ffmpeg(
            [*inputs, "-i", str(audio), "-filter_complex", graph,
             "-map", "[vout]", "-map", f"{audio_index}:a",
             *encoder_args(video, force_cpu=force_cpu), "-r", str(video.fps), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "1", "-movflags", "+faststart", str(out)],
            folder=out.parent,
            log=log,
        )

    try:
        attempt(False)
    except NsError as e:
        if video.encoder != "h264_nvenc" or e.code == "NS-E104":  # a full disk is not an encoder problem
            raise
        if log:
            log.warn("nvenc_failed", code="NS-W401", msg="falling back to libx264")
        attempt(True)
    return out


def sample_times(total_s: float, n: int = 3) -> list[float]:
    """`n` evenly spread moments strictly inside [0, total_s] (at 1/4, 2/4, 3/4 for n = 3)."""
    return [round(total_s * (i + 1) / (n + 1), 3) for i in range(n)]


def shot_at(shots: Sequence[Shot], t: float) -> tuple[Shot, float]:
    """The shot on screen at timeline second `t` and the matching second inside its clip file."""
    start = 0.0
    for s in shots:
        if t < s.end or s is shots[-1]:
            return s, s.ss + max(0.0, min(t, s.end) - start)
        start = s.end
    raise ValueError("no shots")


def backdrop_luma(
    shots: Sequence[Shot], band: Box, size: tuple[int, int], total_s: float, *, n: int = 3
) -> list[float]:
    """Mean luma (16-235) of the frame area behind a caption band at `n` sampled moments (spec 17.2).

    Each moment is read from the clip that is on screen then, scaled to cover the canvas the way `shot_chain`
    does (wide clips in vertical edits are approximated by the same cover crop), cropped to `band`
    (canvas pixels) and measured with FFmpeg `signalstats`.
    """
    if not shots or total_s <= 0:
        return []
    w, h = size
    pre = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    out: list[float] = []
    for t in sample_times(total_s, n):
        shot, src_t = shot_at(shots, t)
        out += sample_luma(Path(shot.path), band, [src_t], pre=pre)
    return out


def fit_caption_contrast(
    *,
    words: Sequence[Word],
    style: CaptionStyle | str,
    opts: AssOptions,
    shots: Sequence[Shot],
    total_s: float,
    size: tuple[int, int],
    ass_path: Path,
    log: NsLogger | None = None,
) -> list[float]:
    """Measure the backdrop behind the caption band and rewrite `ass_path` boxed when contrast is below 4.5:1.

    Returns the luma samples (empty when sampling failed; the check is best effort and never fails a render).
    """
    st = caption_style(style) if isinstance(style, str) else style
    band = caption_band(
        st, opts.variant, avoid_regions=opts.avoid_regions or None, safe_zones=opts.safe_zones,
        anchor_y_pct=opts.anchor_y_pct,
    )
    try:
        luma = backdrop_luma(shots, band, size, total_s)
    except (NsError, OSError) as e:
        if log:
            log.warn("caption_contrast_skipped", str(e)[:200])
        return []
    if luma and ensure_contrast(st, luma) is not st:
        ass_path.write_text(opts.build(words, st, backdrop_luma=luma), encoding="utf-8")
    return luma


def build_short_audio(
    hook: Path | None, body: Path, out_raw: Path, out_final: Path, gap_s: float, log: NsLogger | None = None
) -> Path:
    """Hook + silence + body, loudness-normalized to -14 LUFS / -1.5 dBTP (spec S1).

    The voice stage leaves narration at -16 LUFS with peaks at or under `VOICE_TP`, so this lift of 2 dB is
    linear and adds no limiter of its own; audio from an older voice stage still gets one.
    """
    segments: list[Path | float] = [hook, gap_s, body] if hook else [body]
    assemble_audio(segments, out_raw)
    loudnorm(out_raw, out_final, i=FINAL_LUFS, tp=FINAL_TP, log=log)
    return out_final


class RenderStage:
    """Stage `render`: assemble audio, then render every Short to `shorts/s<k>/short.mp4`."""

    name: str = "render"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("clips/shots.json",)
    outputs: tuple[str, ...] = ("render/report.json",)

    def run(self, ctx: JobContext) -> None:
        """Render each item listed in `clips/shots.json`."""
        shots_by_item: dict[str, Any] = ctx.read_json("clips/shots.json")
        manifest = ctx.read_json("audio/manifest.json")
        treatment = visual_treatment(ctx.combo.visual_treatment)
        report: dict[str, Any] = {}
        for item, entry in shots_by_item.items():
            n = int(item[1:])
            timeline = ctx.read_json(f"captions/{item}.json")
            out_dir = ctx.subdir(f"shorts/{item}")
            hook_entry = manifest.get(f"p{n}_hook")
            audio = build_short_audio(
                Path(hook_entry["path"]) if hook_entry else None,
                Path(manifest[f"p{n}"]["path"]),
                out_dir / "audio_raw.wav",
                out_dir / "audio.wav",
                float(timeline["body_offset_s"]) - float(timeline["hook_s"]),
                ctx.log,
            )
            shots = [Shot.from_json(s) for s in entry["shots"]]
            ass_path = ctx.file(f"captions/{item}.ass")
            luma = fit_caption_contrast(
                words=[Word.from_json(w) for w in timeline["words"]],
                style=timeline.get("style", ctx.combo.caption_style),
                opts=AssOptions.from_json(timeline.get("ass_opts")),
                shots=shots, total_s=float(timeline["audio_s"]), size=VERTICAL, ass_path=ass_path,
                log=ctx.log,
            )
            out = render_video(
                shots=shots,
                audio=audio,
                out=out_dir / "short.mp4",
                size=VERTICAL,
                treatment=treatment,
                video=ctx.runtime.video,
                ass_path=ass_path,
                fonts_dir=FONTS_DIR,
                log=ctx.log,
            )
            report[item] = {
                "path": str(out),
                "backdrop_luma": luma,
                "audio": str(audio),
                "duration_s": round(ff.duration_s(out), 3),
                "shots": len(shots),
                "encoder": ctx.runtime.video.encoder,
            }
            ctx.log.info(
                "render_done", job_id=ctx.job_id, item=item,
                duration_s=report[item]["duration_s"], shots=len(shots),
            )
        ctx.write_json("render/report.json", report)
