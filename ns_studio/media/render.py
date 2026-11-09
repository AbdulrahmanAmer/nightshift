"""FFmpeg filter-graph builders and the render stage (component C6).

One filter graph per video: per-shot trim, scale/crop (or blurred fill), zoompan, concat, colour, vignette,
grain and burned-in ASS captions. Shorts are 1080x1920, masters 1920x1080, 30 fps unless the format a job
draws from says otherwise: a story Short takes its frame size, picture regions, caption anchor, frame rate
and encoder preset from its format (`picture_chains`, `RenderStage`).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import Format, VideoCfg
from ns_studio.context import JobContext
from ns_studio.errors import StudioError
from ns_studio.media.audio import FINAL_LUFS, FINAL_TP, assemble_audio, loudnorm
from ns_studio.media.caption_plan import AssOptions
from ns_studio.media.captions import Box, caption_band, ensure_contrast, sample_luma
from ns_studio.media.diskfull import run_ffmpeg
from ns_studio.media.formats import encoder_preset_of, fps_of, reference_canvas
from ns_studio.media.job_formats import job_format, job_format_entry
from ns_studio.media.layout import SourceSpec, build_regions_graph, covers_canvas
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
from ns_studio.text.story_profile import with_mark

VERTICAL = reference_canvas("vertical")
LANDSCAPE = reference_canvas("landscape")
X264_PRESETS = {
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow",
}


# An apostrophe inside a single-quoted filtergraph value is written as: close the quote, three backslashes
# and a quote (the graph parser leaves one backslash-quote for the option parser), reopen the quote.
_QUOTE_IN_QUOTES = chr(39) + chr(92) * 3 + chr(39) + chr(39)


def escape_filter_path(path: Path) -> str:
    """A path as a single-quoted filtergraph option value: absolute, forward slashes, `:` and `'` escaped.

    The path is made absolute HERE, the one place graph text is built: FFmpeg starts in the folder the job
    files share, so a relative path (a relative `NIGHTSHIFT_HOME`) would be read against that folder.

    An apostrophe used to be deleted, which pointed FFmpeg at a folder that does not exist (a home under
    `C:/Users/O'Brien` failed every render).
    """
    p = Path(os.path.abspath(path)).as_posix().replace(":", "\\:").replace("'", _QUOTE_IN_QUOTES)
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


def shot_chain(
    i: int, shot: Shot, w: int, h: int, fps: int, frames: int, z: ZoomSpec, *, phase: int | None = None
) -> str:
    """Filter chain turning input `i` into `[v<i>]`: exactly `frames` frames of w x h video.

    Wide clips in a vertical edit are centre-cropped to 4:5 over a blurred fill of themselves (spec §7.4);
    everything else is scaled to cover and cropped. `phase` (default `i`) is the shot's place in the whole
    video: the handheld drift of shot k is the same whether it is drawn by the one graph or by the part
    graph of a long render, where its input number is only its place in the part.
    """
    src = f"[{i}:v]fps={fps},setpts=PTS-STARTPTS"
    tail = f"tpad=stop_mode=clone:stop_duration=2,trim=end_frame={frames},setpts=PTS-STARTPTS"
    zoom = zoom_filter(z, w, h, fps, i if phase is None else phase)
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


def ass_filter(ass_path: Path, fonts_dir: Path | None, offset_s: float = 0.0) -> str:
    """The `ass` captions filter; with `offset_s` the clock is moved forward by that many seconds for it.

    A part of a long render starts its own clock at its first frame, while the captions file speaks in
    whole-video time. The step is rounded to whole ticks of the stream's time base: `setpts` cuts a fraction
    off, and a tick can be a whole frame.
    """
    sub = f"ass=filename={escape_filter_path(ass_path)}"
    if fonts_dir is not None and fonts_dir.is_dir():
        sub += f":fontsdir={escape_filter_path(fonts_dir)}"
    if offset_s:
        shift = f"round({offset_s:.6f}/TB)"
        sub = f"setpts=PTS+{shift},{sub},setpts=PTS-{shift}"
    return sub


def post_chain(
    t: VisualTreatment, ass_path: Path | None, fonts_dir: Path | None, *, ass_offset_s: float = 0.0
) -> str:
    """Colour, vignette, grain, captions and pixel format applied to the concatenated video.

    `ass_offset_s` is for a part of a long render: where the part starts in whole-video time (`ass_filter`).
    """
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
        parts.append(ass_filter(ass_path, fonts_dir, ass_offset_s))
    parts.append("format=yuv420p")
    return ",".join(parts)


def cat_chains(
    shots: Sequence[Shot],
    frames: Sequence[int],
    size: tuple[int, int],
    fps: int,
    zoom: ZoomSpec,
    *,
    first_input: int = 0,
    phase_start: int | None = None,
) -> list[str]:
    """The per-shot chains and the concat that joins them into `[cat]`.

    `frames[k]` is the frame count of shot k; shot k is input `first_input + k` and, for the handheld drift,
    place `phase_start + k` of the whole video (default: its input number).
    """
    w, h = size
    phase0 = first_input if phase_start is None else phase_start
    chains = [
        shot_chain(first_input + k, s, w, h, fps, frames[k], zoom, phase=phase0 + k)
        for k, s in enumerate(shots)
    ]
    joined = "".join(f"[v{first_input + k}]" for k in range(len(shots)))
    chains.append(f"{joined}concat=n={len(shots)}:v=1:a=0[cat]")
    return chains


def is_plain(fmt: Format | None) -> bool:
    """True when the picture fills the frame edge to edge: no format, or one `full` region over the canvas."""
    if fmt is None:
        return True
    regions = fmt.layout.regions
    return len(regions) == 1 and regions[0].source == "full" and covers_canvas(fmt, regions[0])


def footage_size(fmt: Format | None, default: tuple[int, int]) -> tuple[int, int]:
    """The size shots are drawn at: the frame for a plain layout, else the size of the `full` region."""
    if fmt is None:
        return default
    if is_plain(fmt):
        return fmt.canvas.w, fmt.canvas.h
    full = next((r for r in fmt.layout.regions if r.source == "full"), None)
    if full is None:
        raise StudioError("NS-E320", f"format {fmt.id!r} has no region that shows the picture (source full)")
    return full.w, full.h


def picture_chains(
    shots: Sequence[Shot],
    frames: Sequence[int],
    size: tuple[int, int],
    fps: int,
    zoom: ZoomSpec,
    fmt: Format | None = None,
    *,
    first_input: int = 0,
    phase_start: int | None = None,
) -> tuple[list[str], str]:
    """The chains that make the picture, and the label (`cat` or `comp`) of the result.

    No format, or a plain layout (one full-frame region): the per-shot chains and their concat at `size`,
    character for character what the renders drew before formats were read. Otherwise the shots are drawn at
    the size of the format's `full` region and `build_regions_graph` places every region on the canvas (a
    blurred fill, a smaller picture, ...); a canvas the first region does not cover starts black for exactly
    the length of the video.
    """
    if fmt is None or is_plain(fmt):
        chains = cat_chains(
            shots, frames, footage_size(fmt, size), fps, zoom,
            first_input=first_input, phase_start=phase_start,
        )
        return chains, "cat"
    w, h = footage_size(fmt, size)
    chains = cat_chains(shots, frames, (w, h), fps, zoom, first_input=first_input, phase_start=phase_start)
    picture = SourceSpec(w, h, label="cat")
    chains += build_regions_graph(
        fmt, {"full": picture, "blur_fill": picture}, out_label="comp", duration_s=sum(frames) / fps, fps=fps
    )
    return chains, "comp"


def video_for_format(video: VideoCfg, fmt: Format) -> VideoCfg:
    """`video` with the frame rate and encoder preset the format's file writes (else the runtime's own)."""
    return video.model_copy(
        update={"fps": fps_of(fmt, video.fps), "preset": encoder_preset_of(fmt, video.preset)}
    )


def build_graph(
    shots: Sequence[Shot],
    edges: Sequence[int],
    size: tuple[int, int],
    fps: int,
    treatment: VisualTreatment,
    ass_path: Path | None,
    fonts_dir: Path | None = None,
    fmt: Format | None = None,
) -> str:
    """The complete filter_complex ending in `[vout]`; input k is the clip of shot k."""
    frames = [edges[i + 1] - edges[i] for i in range(len(shots))]
    chains, label = picture_chains(shots, frames, size, fps, treatment.zoom, fmt)
    chains.append(f"[{label}]{post_chain(treatment, ass_path, fonts_dir)}[vout]")
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


def clip_inputs(shots: Sequence[Shot], frames: Sequence[int], fps: int, base: Path | None) -> list[str]:
    """`-ss S -t T -i FILE` for each shot: T is its frame count in seconds plus 0.5 s for the filter chain."""
    args: list[str] = []
    for s, n in zip(shots, frames, strict=True):
        args += ["-ss", f"{s.ss:.2f}", "-t", f"{n / fps + 0.5:.2f}", "-i", ff.relative_arg(s.path, base)]
    return args


def with_cpu_fallback(video: VideoCfg, log: NsLogger | None, run: Callable[[bool], None]) -> None:
    """`run(False)`; when NVENC fails, `run(True)` once more with libx264 and NS-W401 logged.

    A full disk (NS-E104) is not an encoder problem and is not retried.
    """
    try:
        run(False)
    except NsError as e:
        if video.encoder != "h264_nvenc" or e.code == "NS-E104":
            raise
        if log:
            log.warn("nvenc_failed", code="NS-W401", msg="falling back to libx264")
        run(True)


def part_folder(out: Path) -> Path:
    """Where the parts of a long render live: next to `out`, in the job's render folder."""
    return out.with_name(f"{out.stem}.parts")


def part_path(folder: Path, k: int) -> Path:
    """Part `k`'s video; a fixed-width number keeps every part command the same size."""
    return folder / f"part{k:04d}.mp4"


def join_parts(
    folder: Path,
    parts: Sequence[Path],
    lengths: Sequence[int],
    fps: int,
    extra: list[str],
    out: Path,
    *,
    inputs_after: Sequence[Path] = (),
    log: NsLogger | None = None,
) -> None:
    """Join `parts` into `out` by copying their video through the concat demuxer.

    The list file (`parts.txt`, one `file` and one `duration` line per part, the duration being its frame
    count / fps) is what carries the parts, so the command is the same size for 3 parts and for 300.
    `inputs_after` are further inputs (the audio) named after the list; `extra` is everything between the
    inputs and `out`.
    """
    lst = folder / "parts.txt"
    lst.write_text(ff.concat_list_text(parts, [n / fps for n in lengths]), encoding="utf-8")
    base = ff.common_dir([lst, *inputs_after, out])
    after: list[str] = []
    for path in inputs_after:
        after += ["-i", ff.relative_arg(path, base)]
    run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", ff.relative_arg(lst, base), *after, *extra,
         ff.relative_arg(out, base)],
        folder=out.parent, out=out, log=log, cwd=base,
    )


def clear_parts(folder: Path, *, keep_scripts: bool) -> None:
    """Remove the part videos (the big files) and, after a good render, the rest of the parts folder."""
    if not keep_scripts:
        shutil.rmtree(folder, ignore_errors=True)
        return
    for video in folder.glob("part*.mp4"):
        video.unlink(missing_ok=True)


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
    max_inputs: int = ff.MAX_CLIP_INPUTS,
    fmt: Format | None = None,
) -> Path:
    """Render `shots` + `audio` into `out` (H.264/AAC, faststart); NS-E401 on failure.

    `fmt` (the job's format) lays the picture out on the canvas (`picture_chains`); `size` is its frame.

    When NVENC fails the render is retried once with libx264 and NS-W401 is logged. With more than
    `max_inputs` shots the render is cut into parts of at most that many (`render_in_parts`); the command
    line is then the same size however many shots there are.
    """
    if not shots:
        raise NsError("NS-E401", "render needs at least one shot")
    edges = frame_edges([s.end for s in shots], video.fps)
    frames = [edges[i + 1] - edges[i] for i in range(len(shots))]
    out.parent.mkdir(parents=True, exist_ok=True)
    if len(shots) > max_inputs:
        return render_in_parts(
            shots=shots, frames=frames, audio=audio, out=out, size=size, treatment=treatment, video=video,
            ass_path=ass_path, fonts_dir=fonts_dir, log=log, max_inputs=max_inputs, fmt=fmt,
        )
    graph = build_graph(shots, edges, size, video.fps, treatment, ass_path, fonts_dir, fmt)
    # The command line stays short whatever the install path and the clip count: the graph goes into a script
    # file next to the video, and FFmpeg starts in the folder the files share and names them relative to it.
    base = ff.common_dir([*(s.path for s in shots), audio, out])

    def rel(path: Path | str) -> str:
        return ff.relative_arg(path, base)

    script = ff.filter_complex_args(graph, out.with_name(f"{out.stem}.filtergraph.txt"), log=log, base=base)
    inputs = clip_inputs(shots, frames, video.fps, base)
    audio_index = len(shots)

    def attempt(force_cpu: bool) -> None:
        run_ffmpeg(
            [*inputs, "-i", rel(audio), *script,
             "-map", "[vout]", "-map", f"{audio_index}:a",
             *encoder_args(video, force_cpu=force_cpu), "-r", str(video.fps), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "1", "-movflags", "+faststart", rel(out)],
            folder=out.parent,
            out=out,
            log=log,
            cwd=base,
        )

    with_cpu_fallback(video, log, attempt)
    return out


def render_in_parts(
    *,
    shots: Sequence[Shot],
    frames: Sequence[int],
    audio: Path,
    out: Path,
    size: tuple[int, int],
    treatment: VisualTreatment,
    video: VideoCfg,
    ass_path: Path | None,
    fonts_dir: Path | None,
    log: NsLogger | None,
    max_inputs: int,
    fmt: Format | None = None,
) -> Path:
    """The same video as one command, rendered in parts of at most `max_inputs` shots and joined.

    Each part is the whole pipeline over its shots (the same per-shot chains, concat, colour, grain and
    captions), written as video only into `<out>.parts/`; the captions clock is moved by the part's start (see
    `post_chain`). Then one command copies the parts together through a list file and adds the audio. Every
    command carries at most `max_inputs` clips, so none grows with the total. A part that fails on NVENC sends
    the whole render back to libx264: parts of two encoders cannot be joined by copying.
    """
    fps = video.fps
    folder = part_folder(out)
    folder.mkdir(parents=True, exist_ok=True)
    ranges = ff.group_ranges(len(shots), max_inputs)
    starts = [sum(frames[:a]) for a, _b in ranges]
    lengths = [sum(frames[a:b]) for a, b in ranges]
    if log:
        log.info(
            "render_parts", f"{len(shots)} clips in {len(ranges)} parts of at most {max_inputs}",
            clips=len(shots), parts=len(ranges), max_inputs=max_inputs, folder=str(folder),
        )

    def attempt(force_cpu: bool) -> None:
        parts: list[Path] = []
        for k, (a, b) in enumerate(ranges):
            part = part_path(folder, k)
            mine = shots[a:b]
            chains, label = picture_chains(mine, frames[a:b], size, fps, treatment.zoom, fmt, phase_start=a)
            post = post_chain(treatment, ass_path, fonts_dir, ass_offset_s=starts[k] / fps)
            chains.append(f"[{label}]{post}[vout]")
            base = ff.common_dir([*(s.path for s in mine), part])
            script = ff.filter_complex_args(
                ";".join(chains), part.with_name(f"{part.stem}.filtergraph.txt"), log=log, base=base
            )
            run_ffmpeg(
                [*clip_inputs(mine, frames[a:b], fps, base), *script, "-map", "[vout]", "-an",
                 *encoder_args(video, force_cpu=force_cpu), "-r", str(fps), "-pix_fmt", "yuv420p",
                 ff.relative_arg(part, base)],
                folder=folder, out=part, log=log, cwd=base,
            )
            parts.append(part)
        join_parts(
            folder, parts, lengths, fps,
            ["-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
             "-ac", "1", "-movflags", "+faststart"],
            out, inputs_after=[audio], log=log,
        )

    try:
        with_cpu_fallback(video, log, attempt)
    except BaseException:
        clear_parts(folder, keep_scripts=True)  # the graphs stay: they are what a failure report needs
        raise
    clear_parts(folder, keep_scripts=False)
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
        anchor_y_pct=opts.anchor_y_pct, canvas=opts.canvas,
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
    hook: Path | None,
    body: Path,
    out_raw: Path,
    out_final: Path,
    gap_s: float,
    log: NsLogger | None = None,
    *,
    close: Path | None = None,
    close_gap_s: float = 0.0,
) -> Path:
    """Hook + silence + body (+ silence + closing line), normalized to -14 LUFS / -1.5 dBTP (spec S1).

    The voice stage leaves narration at -16 LUFS with peaks at or under `VOICE_TP`, so this lift of 2 dB is
    linear and adds no limiter of its own; audio from an older voice stage still gets one.
    """
    segments: list[Path | float] = [hook, gap_s, body] if hook else [body]
    if close:
        segments += [close_gap_s, close]
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
        fmt = job_format(ctx, "short")
        chosen = job_format_entry(ctx, "short")
        canvas = (fmt.canvas.w, fmt.canvas.h)
        video = video_for_format(ctx.runtime.video, fmt)
        report: dict[str, Any] = {}
        for item, entry in shots_by_item.items():
            n = int(item[1:])
            timeline = ctx.read_json(f"captions/{item}.json")
            out_dir = ctx.subdir(f"shorts/{item}")
            hook_entry = manifest.get(f"p{n}_hook")
            # the closing line the captions timed (the same gap): without it in the timeline it is not played
            close_entry = manifest.get(f"p{n}_close") if float(timeline.get("close_s", 0.0)) > 0 else None
            audio = build_short_audio(
                Path(hook_entry["path"]) if hook_entry else None,
                Path(manifest[f"p{n}"]["path"]),
                out_dir / "audio_raw.wav",
                out_dir / "audio.wav",
                float(timeline["body_offset_s"]) - float(timeline["hook_s"]),
                ctx.log,
                close=Path(close_entry["path"]) if close_entry else None,
                close_gap_s=float(timeline.get("close_gap_s", 0.0)),
            )
            shots = [Shot.from_json(s) for s in entry["shots"]]
            ass_path = ctx.file(f"captions/{item}.ass")
            luma = fit_caption_contrast(
                words=[Word.from_json(w) for w in timeline["words"]],
                style=timeline.get("style", ctx.combo.caption_style),
                opts=AssOptions.from_json(timeline.get("ass_opts")),
                shots=shots, total_s=float(timeline["audio_s"]), size=canvas, ass_path=ass_path,
                log=ctx.log,
            )
            out = render_video(
                shots=shots,
                audio=audio,
                out=out_dir / "short.mp4",
                size=canvas,
                treatment=treatment,
                video=video,
                ass_path=ass_path,
                fonts_dir=FONTS_DIR,
                log=ctx.log,
                fmt=fmt,
            )
            report[item] = {
                "path": str(out),
                "backdrop_luma": luma,
                "audio": str(audio),
                "duration_s": round(ff.duration_s(out), 3),
                "shots": len(shots),
                "encoder": ctx.runtime.video.encoder,
                "format": fmt.id,
                "format_why": chosen.get("why"),
                "canvas": list(canvas),
            }
            ctx.log.info(
                "render_done", job_id=ctx.job_id, item=item,
                duration_s=report[item]["duration_s"], shots=len(shots), format=fmt.id,
            )
        ctx.write_json("render/report.json", with_mark(ctx, report))
