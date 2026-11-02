"""Thumbnails: high-contrast frame pick and Pillow text layouts (component C6, M2).

Pillow is imported lazily so the module imports without it; frame picking needs only FFmpeg and numpy.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.textstats import WORD_PATTERN
from ns_studio.context import JobContext
from ns_studio.media.presets import FONTS_DIR, ThumbnailLayout, load_thumbnail_layouts
from ns_studio.stages import Tenant

SIZE = (1280, 720)
STOPWORDS = frozenset(
    "a an the of to in on at and or but is was were be i my me it its that this these those for with"
    " from by as".split()
)


def parse_pgm(data: bytes) -> np.ndarray:
    """Pixels of a binary PGM (P5) file as a 2-D uint8 array."""
    pos = 0
    tokens: list[bytes] = []
    while len(tokens) < 4:
        while data[pos : pos + 1].isspace():
            pos += 1
        if data[pos : pos + 1] == b"#":
            while data[pos : pos + 1] not in (b"\n", b""):
                pos += 1
            continue
        start = pos
        while pos < len(data) and not data[pos : pos + 1].isspace():
            pos += 1
        tokens.append(data[start:pos])
    pos += 1
    w, h = int(tokens[1]), int(tokens[2])
    return np.frombuffer(data[pos : pos + w * h], dtype=np.uint8).reshape(h, w)


def luminance_variance(video: Path, t: float, scratch: Path) -> float:
    """Variance of the 160x90 grey frame at `t` seconds: flat or black frames score near zero."""
    out = scratch / f"probe_{t:08.2f}.pgm"
    ff.run(["-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-vf", "scale=160:90,format=gray",
            "-update", "1", str(out)])
    return float(np.var(parse_pgm(out.read_bytes()).astype(np.float64)))


def sample_times(duration: float, samples: int = 24) -> list[float]:
    """Evenly spaced sample times between 8 % and 92 % of the video."""
    lo, hi = duration * 0.08, duration * 0.92
    if samples <= 1:
        return [duration / 2]
    return [lo + (hi - lo) * i / (samples - 1) for i in range(samples)]


def pick_frame_times(video: Path, scratch: Path, count: int = 3, samples: int = 24) -> list[float]:
    """The `count` times with the highest luminance variance, at least 8 % of the video apart."""
    dur = ff.duration_s(video)
    scratch.mkdir(parents=True, exist_ok=True)
    times = sample_times(dur, samples)
    scored = sorted(((luminance_variance(video, t, scratch), t) for t in times), reverse=True)
    picked: list[float] = []
    for _v, t in scored:
        if all(abs(t - p) >= dur * 0.08 for p in picked):
            picked.append(t)
        if len(picked) == count:
            break
    for _v, t in scored:  # short videos: fill up even if closer than the spacing
        if len(picked) >= count:
            break
        if t not in picked:
            picked.append(t)
    return sorted(picked)


def extract_frame(video: Path, t: float, out_png: Path) -> Path:
    """One 1280x720 frame at `t` seconds."""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    ff.run(["-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-vf",
            f"scale={SIZE[0]}:{SIZE[1]}:force_original_aspect_ratio=increase,crop={SIZE[0]}:{SIZE[1]}",
            "-update", "1", str(out_png)])
    return out_png


def thumb_text(title: str, max_words: int = 4) -> str:
    """At most `max_words` upper-case words from a title, skipping stop words (falls back to any words)."""
    words = re.findall(WORD_PATTERN, title)
    keep = [w for w in words if w.lower() not in STOPWORDS] or words
    return " ".join(keep[:max_words]).upper()


def _hex(c: str) -> tuple[int, int, int]:
    h = c.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _font(pil_font: Any, px: int) -> Any:
    candidates = [p for p in sorted(FONTS_DIR.glob("Anton*.ttf"))] if FONTS_DIR.is_dir() else []
    candidates += [Path("impact.ttf"), Path("arialbd.ttf"), Path("arial.ttf")]
    for c in candidates:
        try:
            return pil_font.truetype(str(c), px)
        except OSError:
            continue
    return pil_font.load_default(px)


def render_thumbnail(frame: Path, text: str, layout: ThumbnailLayout, out: Path) -> Path:
    """Draw `text` (at most 4 words) over `frame` using `layout`; writes a 1280x720 JPEG."""
    if len(text.split()) > 4:
        raise NsError("NS-E401", f"thumbnail text has more than 4 words: {text!r}")
    try:
        image_mod = importlib.import_module("PIL.Image")
        draw_mod = importlib.import_module("PIL.ImageDraw")
        font_mod = importlib.import_module("PIL.ImageFont")
    except ImportError as e:
        raise NsError("NS-E401", "Pillow is required for thumbnails") from e
    img = image_mod.open(frame).convert("RGB").resize(SIZE)
    w, h = SIZE
    px = int(h * layout.size_pct)
    font = _font(font_mod, px)
    # darkening gradient on the side of the text
    shade = image_mod.new("L", SIZE, 0)
    sd = draw_mod.Draw(shade)
    for y in range(h):
        frac = y / h
        k = {"bottom_left": frac, "top_left": 1 - frac, "center": 1 - abs(frac - 0.5) * 2}[layout.position]
        sd.line([(0, y), (w, y)], fill=int(255 * layout.shade * k))
    img.paste(image_mod.new("RGB", SIZE, (0, 0, 0)), (0, 0), shade)
    draw = draw_mod.Draw(img)
    lines = _wrap(text, 2 if len(text.split()) > 2 else 1)
    line_h = int(px * 1.05)
    block_h = line_h * len(lines)
    margin = int(w * 0.05)
    if layout.position == "bottom_left":
        y0 = h - margin - block_h
    elif layout.position == "top_left":
        y0 = margin
    else:
        y0 = (h - block_h) // 2
    stroke = max(1, int(h * layout.stroke_pct))
    for i, line in enumerate(lines):
        if layout.position == "center":
            tw = draw.textlength(line, font=font)
            x = (w - tw) / 2
        else:
            x = margin
        draw.text((x, y0 + i * line_h), line, font=font, fill=_hex(layout.color),
                  stroke_width=stroke, stroke_fill=_hex(layout.stroke))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=90)
    return out


def _wrap(text: str, lines: int) -> list[str]:
    words = text.split()
    if lines <= 1 or len(words) <= 1:
        return [text]
    half = (len(words) + 1) // 2
    return [" ".join(words[:half]), " ".join(words[half:])]


def make_thumbnails(
    frames: Sequence[Path], text: str, out_dir: Path, layouts: Sequence[ThumbnailLayout] | None = None
) -> list[Path]:
    """Three thumbnails `thumb_1.jpg` .. `thumb_3.jpg`: frame i with layout i."""
    use = list(layouts) if layouts is not None else list(load_thumbnail_layouts().values())
    return [
        render_thumbnail(frames[i % len(frames)], text, use[i % len(use)], out_dir / f"thumb_{i + 1}.jpg")
        for i in range(3)
    ]


class ThumbnailsStage:
    """Stage `thumbnails` (M14): pick 3 high-contrast frames of the master and overlay a short text."""

    name: str = "thumbnails"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("long/master.mp4", "seo_long.json")
    outputs: tuple[str, ...] = ("long/thumb_1.jpg", "long/thumb_2.jpg", "long/thumb_3.jpg")

    def run(self, ctx: JobContext) -> None:
        """Write `long/thumb_[1-3].jpg`."""
        master = ctx.file("long/master.mp4")
        scratch = ctx.subdir("long/thumb_work")
        seo = ctx.read_json("seo_long.json") if ctx.file("seo_long.json").exists() else {}
        text = thumb_text(str(seo.get("thumbnail_text") or seo.get("title") or ctx.channel.display_name))
        times = pick_frame_times(master, scratch)
        frames = [extract_frame(master, t, scratch / f"frame_{i + 1}.png") for i, t in enumerate(times)]
        style = str(ctx.series.get("design", {}).get("thumbnail_style") or "auto")
        layouts = load_thumbnail_layouts()
        make_thumbnails(frames, text, ctx.file("long"), [layouts[style]] * 3 if style in layouts else None)
        ctx.log.info("thumbnails_done", job_id=ctx.job_id, times=[round(t, 2) for t in times], text=text)
