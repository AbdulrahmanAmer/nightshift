"""Generic layout engine (spec 17.1): FFmpeg filter fragments from a format preset's `layout.regions`.

`build_regions_graph(format, sources)` places every region of a `Format` on its canvas: the source it names
(`full`, `facecam`, `face:<n>`, `screen`, `blur_fill`) is cropped, scaled and overlaid at the region's
rectangle. Split screens, picture-in-picture and new aspect ratios are therefore new YAML files, not code.

Callers describe the *input* with `SourceSpec`s keyed by the region source names, for example the facecam
rectangle found by detection or a face-centred crop. Pure functions only; nothing here runs FFmpeg.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from ns_common.models import Format, FormatRegion

Fit = Literal["cover", "contain"]
CROP_TOLERANCE = 0.02  # an explicit crop within 2 % of the region's aspect is scaled as is (no extra trim)


@dataclass(frozen=True)
class SourceSpec:
    """How one region source maps onto an input stream.

    `w`/`h` are the pixel size of the input stream behind pad `label`. `crop` (x, y, w, h in source pixels) is
    the part of the frame the region shows (default: the whole frame). `fit` is `cover` (scale to fill,
    trimming the excess; the default) or `contain` (letterbox on black; the default for the `screen` source).
    `x_expr` is an FFmpeg expression of `t` for the crop's x position, which makes the crop window slide
    (smooth face tracking); it needs `crop` for the window size and row.
    """

    w: int
    h: int
    label: str = "src"
    crop: tuple[int, int, int, int] | None = None
    fit: Fit | None = None
    x_expr: str | None = None


def even(v: float) -> int:
    """`v` rounded to the nearest integer and then down to an even number (at least 2)."""
    n = int(round(v))
    return max(2, n - (n % 2))


# ---------- region geometry ----------
def covers_canvas(fmt: Format, region: FormatRegion) -> bool:
    """True when `region` fills the whole canvas."""
    return (region.x, region.y, region.w, region.h) == (0, 0, fmt.canvas.w, fmt.canvas.h)


def fit_facecam_height(fmt: Format, cam_w: int, cam_h: int) -> Format:
    """`fmt` with each `facecam` region shrunk to the camera's aspect when that is shorter than the region.

    The region's height is the maximum; a wide camera needs less. Regions that started at the camera
    region's bottom edge move up with it, and those that reached the canvas bottom grow to keep reaching it.
    """
    regions = list(fmt.layout.regions)
    for i, r in enumerate(list(regions)):
        if r.source != "facecam" or cam_w <= 0 or cam_h <= 0:
            continue
        new_h = min(even(r.w * cam_h / cam_w), r.h - (r.h % 2))
        if new_h >= r.h:
            continue
        old_bottom, delta = r.y + r.h, r.h - new_h
        regions[i] = r.model_copy(update={"h": new_h})
        for j, other in enumerate(regions):
            if j == i or other.y != old_bottom:
                continue
            reaches_bottom = other.y + other.h == fmt.canvas.h
            regions[j] = other.model_copy(
                update={"y": r.y + new_h, "h": other.h + delta if reaches_bottom else other.h}
            )
    layout = fmt.layout.model_copy(update={"regions": regions})
    return fmt.model_copy(update={"layout": layout})


def caption_anchor_pct(fmt: Format) -> float:
    """Caption anchor in % of the canvas height: the seam below the first region, or the preset's value."""
    if fmt.layout.seam_captions and len(fmt.layout.regions) > 1:
        first = fmt.layout.regions[0]
        return 100.0 * (first.y + first.h) / fmt.canvas.h
    return float(fmt.captions.anchor_y_pct)


# ---------- chains ----------
def _fit_window(
    crop: tuple[int, int, int, int], aspect: float
) -> tuple[int, int, int, int]:
    """The largest centred window of `aspect` inside `crop`; `crop` itself when already within tolerance."""
    x, y, w, h = crop
    if abs((w / h) / aspect - 1.0) <= CROP_TOLERANCE:
        return crop
    if w / h > aspect:
        nw = min(w, even(h * aspect))
        return x + (w - nw) // 2, y, nw, h
    nh = min(h, even(w / aspect))
    return x, y + (h - nh) // 2, w, nh


def region_chain(region: FormatRegion, spec: SourceSpec) -> str:
    """The filter chain (without pads) that turns the source into the region's w x h picture."""
    w, h = region.w, region.h
    if region.source == "blur_fill":
        qw, qh = max(2, (w // 4) - (w // 4) % 2), max(2, (h // 4) - (h // 4) % 2)
        return (
            f"scale={qw}:{qh}:force_original_aspect_ratio=increase,crop={qw}:{qh},boxblur=10:2,"
            f"eq=brightness=-0.2,scale={w}:{h}"
        )
    fit: Fit = spec.fit or ("contain" if region.source == "screen" else "cover")
    crop = spec.crop
    if fit == "contain":
        pre = f"crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]}," if crop else ""
        return (
            f"{pre}scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    if spec.x_expr is not None and crop is not None:
        cx, cy, cw, ch = crop
        window = f"crop=w={cw}:h={ch}:x='clip({spec.x_expr},0,{max(0, spec.w - cw)})':y={cy}:exact=1"
    else:
        cx, cy, cw, ch = _fit_window(crop or (0, 0, spec.w, spec.h), w / h)
        window = f"crop={cw}:{ch}:{cx}:{cy}"
    return f"{window},scale={w}:{h}:flags=lanczos"


def _spec_for(region: FormatRegion, sources: Mapping[str, SourceSpec]) -> SourceSpec:
    spec = sources.get(region.source) or sources.get("full")
    if spec is None:
        raise ValueError(f"no source given for region {region.name!r} (source {region.source!r})")
    return spec


def build_regions_graph(
    fmt: Format,
    sources: Mapping[str, SourceSpec],
    *,
    out_label: str = "v0",
    duration_s: float | None = None,
    fps: int | None = None,
) -> list[str]:
    """`filter_complex` fragments drawing every region of `fmt` onto its canvas; the result is `[out_label]`.

    The input pads named by the `SourceSpec`s (default `[src]`) must be produced by the caller. A source
    used by several regions is split first. Regions are overlaid in list order over the first region when
    it fills the canvas, else over a black canvas (`duration_s` ends that canvas with the video; `fps` is
    its frame rate, the format's own `canvas.fps` when not given: a render passes the rate its shots run at).
    """
    regions = fmt.layout.regions
    specs = [_spec_for(r, sources) for r in regions]
    uses = Counter(s.label for s in specs)
    fragments: list[str] = []
    taken: Counter[str] = Counter()
    inputs: list[str] = []
    for label, n in uses.items():
        if n > 1:
            fragments.append(f"[{label}]split={n}" + "".join(f"[{label}_{k}]" for k in range(n)))
    for spec in specs:
        if uses[spec.label] > 1:
            inputs.append(f"{spec.label}_{taken[spec.label]}")
            taken[spec.label] += 1
        else:
            inputs.append(spec.label)
    pads = [f"r{i}" for i in range(len(regions))]
    for region, spec, src, pad in zip(regions, specs, inputs, pads, strict=True):
        fragments.append(f"[{src}]{region_chain(region, spec)}[{pad}]")
    if len(regions) == 1 and covers_canvas(fmt, regions[0]):
        fragments[-1] = fragments[-1].rsplit("[", 1)[0] + f"[{out_label}]"
        return fragments
    if covers_canvas(fmt, regions[0]):
        base, rest = pads[0], list(zip(regions[1:], pads[1:], strict=True))
    else:
        d = f":d={duration_s:.3f}" if duration_s else ""
        fragments.append(f"color=c=black:s={fmt.canvas.w}x{fmt.canvas.h}:r={fps or fmt.canvas.fps}{d}[base]")
        base, rest = "base", list(zip(regions, pads, strict=True))
    for k, (region, pad) in enumerate(rest):
        last = k == len(rest) - 1
        target = out_label if last else f"o{k}"
        fragments.append(f"[{base}][{pad}]overlay={region.x}:{region.y}:shortest=1[{target}]")
        base = target
    return fragments
