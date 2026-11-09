"""Golden render commands and caption bands of the shipped long and square presets (package P10b).

Run from the repo root on a checkout BEFORE full videos read their format:

    python -m tests.golden.p10b.make_golden           # writes tests/golden/p10b/*.txt

The files here were generated from origin/main d890afa (P10a merged, P10b not started). They say what the
shipped presets must keep producing once the master and series parts draw from `long_16x9`, and what the
shipped `square_1x1` already gives through the P10a route (a Short). Nothing needs FFmpeg: the master
chapter render runs against a recording stand-in for `ns_common.ffmpeg.run`, and the texts are the FFmpeg
arguments, the filter graph the command reads and the title-card caption file.

`tests/golden/p10/` holds the pure graph and caption texts of the same presets; this folder adds the pieces
of the chapter render that the format change touches (the colour source of the title card, the encoder and
frame-rate arguments, the parts of a long chapter) and the caption bands that the contrast check samples.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from ns_common import ffmpeg as ff
from ns_studio.media import longform, render
from ns_studio.media.caption_plan import AssOptions
from ns_studio.media.captions import build_ass, caption_band
from ns_studio.media.formats import get_format
from ns_studio.media.master import ChapterTiming
from ns_studio.media.presets import (
    FONTS_DIR,
    chapter_card,
    load_caption_styles,
    visual_treatment,
)
from ns_studio.media.shotplan import Shot, frame_edges
from tests.golden.p10 import make_golden as p10
from tests.helpers import make_combo, make_ctx

GOLDEN_DIR = Path(__file__).resolve().parent
FPS = 30


class Recorder:
    """Stands in for `ns_common.ffmpeg.run`: records each command and the filter graph its script file holds.

    The graph is read when the command is recorded: a render in parts removes the scripts of a good render.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def run(self, args: list[str], *, cwd: Path | None = None, **_kw: Any) -> None:
        graph = ""
        for option in ("-filter_complex_script", "-/filter_complex"):
            if option in args:
                assert cwd is not None
                graph = (cwd / args[args.index(option) + 1]).read_text(encoding="utf-8")
        self.calls.append((list(args), graph))


def _shots(ctx_cache: Path, first: float) -> list[Shot]:
    """Five landscape shots from `first` on, for a chapter whose audio starts at `first`."""
    ends = [2.4, 4.7, 7.5, 9.6, 12.0]
    out: list[Shot] = []
    start = first
    for i, end in enumerate(ends):
        path = ctx_cache / "pexels" / f"{1000 + i}.mp4"
        out.append(
            Shot(0, start, first + end, "pexels", f"id{i}", str(path), 0.5 * i, 1920, 1080, "u", "l", "kw")
        )
        start = first + end
    return out


def _command_text(rec: Recorder) -> str:
    """Every recorded command: its arguments one per line, then the graph its script file held."""
    blocks = [
        f"# command {n}\n" + "\n".join(args) + ("\n# graph\n" + graph if graph else "")
        for n, (args, graph) in enumerate(rec.calls)
    ]
    return "\n".join(blocks) + "\n"


def master_cases(root: Path) -> dict[str, str]:
    """The chapter render commands of the long preset: with a card, without, and in parts."""
    out: dict[str, str] = {}
    ctx = make_ctx(root / "home", fmt="master_first", combo=make_combo(caption_style="clean_white"))
    card = chapter_card("midnight")
    ctx.subdir("captions")
    ctx.subdir("long")
    rec = Recorder()
    cases = [
        ("card_cinematic_teal", "cinematic_teal", 3.0, 15.0, True, None),
        ("nocard_dark_slowzoom", "dark_slowzoom", 0.0, 12.0, False, None),
        ("card_parts_handheld_grit", "handheld_grit", 3.0, 15.0, True, 2),
    ]
    with (
        mock.patch.object(ff, "run", rec.run),
        mock.patch.object(ff, "ffmpeg_exe", lambda: str(root / "bin" / "ffmpeg.exe")),
        mock.patch.object(ff, "_reads_filter_script_option", lambda _exe: True),
        mock.patch.object(longform, "fit_chapter_contrast", lambda *a, **k: []),
    ):
        for stem, treatment, audio_start, end, with_card, max_inputs in cases:
            rec.calls.clear()
            t = ChapterTiming(1, "One", 0.0, audio_start, end)
            shots = _shots(ctx.paths.cache_clips, audio_start)
            kw: dict[str, Any] = {} if max_inputs is None else {"max_inputs": max_inputs}
            longform.render_segment(
                ctx, t, shots, visual_treatment(treatment), card, ctx.subdir("long") / f"seg_{stem}.mp4", **kw
            )
            text = _command_text(rec)
            if with_card:
                text += "# card captions\n" + ctx.file("captions/card_c1.ass").read_text(encoding="utf-8")
            out[f"master_command_{stem}"] = text.replace(ctx.job_id, "<JOBID>")
    return out


def band_cases() -> dict[str, str]:
    """The caption band (what the contrast check samples) of the long and square presets, per style."""
    out: dict[str, str] = {}
    square = get_format("square_1x1")
    for st in ("clean_white", "minimal_lower"):
        opts = AssOptions(variant="landscape", safe_zones=(54, 54))
        out[f"long_band_{st}"] = repr(caption_band(st, opts.variant, safe_zones=opts.safe_zones)) + "\n"
    for st in sorted(load_caption_styles()):
        o = AssOptions.for_format(square)
        band = caption_band(
            st, o.variant, safe_zones=o.safe_zones, anchor_y_pct=o.anchor_y_pct, canvas=o.canvas
        )
        out[f"square_band_{st}"] = repr(band) + "\n"
    return out


def square_cases(root: Path) -> dict[str, str]:
    """The shipped `square_1x1` as a Short draws it today (P10a): the story graph and the caption file."""
    out: dict[str, str] = {}
    fmt = get_format("square_1x1")
    canvas = (fmt.canvas.w, fmt.canvas.h)
    shots = p10.fixed_shots()
    edges = frame_edges([s.end for s in shots], FPS)
    ass_path = root / "captions" / "s1.ass"
    for name in ("cinematic_teal", "dark_slowzoom"):
        out[f"square_story_graph_{name}"] = render.build_graph(
            shots, edges, canvas, FPS, visual_treatment(name), ass_path, FONTS_DIR, fmt
        )
    empty = root / "no-fonts"
    empty.mkdir(parents=True, exist_ok=True)
    words = [w for w in p10.fixed_words() if w.sentence_idx >= 0]
    opts = AssOptions.for_format(fmt, emphasis={0: "doorbell"}, mask_profanity=False)
    for st in sorted(load_caption_styles()):
        out[f"square_ass_{st}"] = build_ass(words, st, fonts_dir=empty, **opts.kwargs())
    return out


def cases(root: Path) -> dict[str, str]:
    """{file stem: text} for every case, normalised so Windows and Linux compare equal."""
    built = {**master_cases(root), **band_cases(), **square_cases(root)}
    return {k: p10.normalise(v, root) for k, v in built.items()}


def main() -> None:
    """Write every case into this folder (run on the code the golden files are meant to pin)."""
    with tempfile.TemporaryDirectory(prefix="p10b-golden-", ignore_cleanup_errors=True) as d:
        os.environ["NIGHTSHIFT_HOME"] = str(Path(d) / "home")
        (Path(d) / "home").mkdir(parents=True, exist_ok=True)
        built = cases(Path(d))
    for stale in GOLDEN_DIR.glob("*.txt"):
        stale.unlink()
    for stem, text in built.items():
        (GOLDEN_DIR / f"{stem}.txt").write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(built)} golden files to {GOLDEN_DIR}")


if __name__ == "__main__":
    sys.exit(main())
