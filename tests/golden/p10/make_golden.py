"""Golden filter graphs and ASS files of the shipped presets (package P10, the reverse of the format change).

Run from the repo root on a checkout BEFORE the formats-as-data change:

    python -m tests.golden.p10.make_golden            # writes tests/golden/p10/*.txt

`cases()` builds one text per case from fixed inputs, through the functions the renders call. The files in
this folder were generated from origin/main d50d4ad (the code before package P10a); they say what the shipped
presets must keep producing. Every function used here is pure: no FFmpeg, no network.

Paths are normalised before writing and before comparing: the temporary folder becomes `<TMP>` and the shipped
fonts folder `<FONTS>`, so Windows and Linux compare equal.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from ns_common.models import ClipLayout, FaceFrame, Rect, VideoCfg
from ns_studio.clipper import layout as clip_layout
from ns_studio.clipper import render as clip_render
from ns_studio.media import longform, render
from ns_studio.media.caption_plan import AssOptions, emphasis_from_list, safe_zones_for
from ns_studio.media.captions import build_ass, caption_band  # noqa: F401  (caption_band: used by callers)
from ns_studio.media.presets import (
    FONTS_DIR,
    load_caption_styles,
    load_chapter_cards,
    load_visual_treatments,
    visual_treatment,
)
from ns_studio.media.shotplan import Shot, frame_edges
from ns_studio.media.transcribe import Word

GOLDEN_DIR = Path(__file__).resolve().parent
FPS = 30
CAM = Rect(x=944, y=16, w=320, h=180)
SRC_W, SRC_H = 1280, 720


# ---------- fixed inputs ----------
def fixed_shots() -> list[Shot]:
    """Five shots: portrait and wide clips (a wide clip in a vertical edit takes the blurred-fill route)."""
    sizes = [(1080, 1920), (1920, 1080), (1080, 1920), (1920, 1080), (1080, 1920)]
    ends = [2.4, 4.7, 7.5, 9.6, 12.0]
    shots: list[Shot] = []
    start = 0.0
    for i, ((w, h), end) in enumerate(zip(sizes, ends, strict=True)):
        shots.append(
            Shot(i // 2, start, end, "pexels", f"id{i}", f"clips/c{i}.mp4", 0.5 * i, w, h, "u", "l", "kw")
        )
        start = end
    return shots


def fixed_words() -> list[Word]:
    """Two sentences, a hook word run (sentence -1), a closing word (sentence -2) and an emphasis target."""
    spec = [
        ("Someone", 0.0, 0.4, -1),
        ("is", 0.4, 0.6, -1),
        ("watching.", 0.6, 1.3, -1),
        ("The", 1.6, 1.8, 0),
        ("doorbell", 1.8, 2.3, 0),
        ("camera", 2.3, 2.8, 0),
        ("pings", 2.8, 3.2, 0),
        ("every", 3.2, 3.5, 0),
        ("night.", 3.5, 4.1, 0),
        ("I", 4.8, 4.9, 1),
        ("stopped", 4.9, 5.3, 1),
        ("checking", 5.3, 5.8, 1),
        ("the", 5.8, 5.9, 1),
        ("clips.", 5.9, 6.5, 1),
        ("Watch", 7.2, 7.5, -2),
        ("the", 7.5, 7.6, -2),
        ("full", 7.6, 7.9, -2),
        ("story.", 7.9, 8.4, -2),
    ]
    return [Word(w, a, b, s) for w, a, b, s in spec]


def clip_words() -> list[Word]:
    """Clip transcript words (clip-relative times)."""
    return [
        Word(w, round(0.45 * i, 2), round(0.45 * i + 0.4, 2), i // 4)
        for i, w in enumerate(
            "Hello there friend. This is truly great. Now we begin the lesson today.".split()
        )
    ]


def rich_layout(name: str) -> ClipLayout:
    left, right = Rect(x=150, y=200, w=100, h=100), Rect(x=850, y=200, w=100, h=100)
    faces = [FaceFrame(t=float(i), boxes=[left, right], speaking=0 if i < 6 else 1) for i in range(12)]
    return ClipLayout(template=name, facecam=CAM, faces=faces)


# ---------- normalisation ----------
def _forms(root: Path) -> list[str]:
    import os

    posix = Path(os.path.abspath(root)).as_posix()
    return [posix.replace(":", "\\:"), posix]


def normalise(text: str, tmp: Path) -> str:
    """Replace the temporary folder and the fonts folder (escaped or plain) by `<TMP>` / `<FONTS>`."""
    pairs = [(f, "<TMP>") for f in _forms(tmp)] + [(f, "<FONTS>") for f in _forms(FONTS_DIR)]
    # the fonts folder may sit inside the temp folder on no machine, but longest-first keeps it safe
    for form, label in sorted(pairs, key=lambda p: -len(p[0])):
        text = text.replace(form, label)
    return text


# ---------- cases ----------
def cases(tmp: Path) -> dict[str, str]:
    """{file stem: text} for every golden case, built through the code the renders call today."""
    out: dict[str, str] = {}
    empty_fonts = tmp / "no-fonts"
    empty_fonts.mkdir(parents=True, exist_ok=True)
    shots = fixed_shots()
    edges = frame_edges([s.end for s in shots], FPS)
    ass_path = tmp / "captions" / "s1.ass"
    treatments = sorted(load_visual_treatments())
    styles = sorted(load_caption_styles())

    # story Short graph, one per treatment
    for name in treatments:
        out[f"story_graph_{name}"] = render.build_graph(
            shots, edges, render.VERTICAL, FPS, visual_treatment(name), ass_path, FONTS_DIR
        )

    # Short ASS (story Shorts) and derived Short ASS, one per caption style
    words = fixed_words()
    story_words = [w for w in words if w.sentence_idx >= 0]
    story_opts = AssOptions(
        variant="vertical",
        safe_zones=safe_zones_for("vertical"),
        emphasis={0: "doorbell"},
        mask_profanity=False,
    )
    derived_opts = AssOptions(
        variant="vertical", safe_zones=safe_zones_for("vertical"), emphasis={1: "clips"}, mask_profanity=True
    )
    for st in styles:
        out[f"short_ass_{st}"] = build_ass(story_words, st, fonts_dir=empty_fonts, **story_opts.kwargs())
        out[f"derived_short_ass_{st}"] = build_ass(words, st, fonts_dir=empty_fonts, **derived_opts.kwargs())

    # master segment graph, with and without a title card, one per treatment
    long_edges = frame_edges([s.end for s in shots], FPS)
    chapter_ass = tmp / "captions" / "long_c1.ass"
    card_path = tmp / "captions" / "card1.ass"
    for name in treatments:
        for with_card in (False, True):
            out[f"master_graph_{name}_{'card' if with_card else 'nocard'}"] = longform.build_segment_graph(
                shots,
                long_edges,
                90 if with_card else 0,
                FPS,
                visual_treatment(name),
                chapter_ass,
                card_path if with_card else None,
                FONTS_DIR,
            )

    # master (landscape) captions for the two caption styles a master uses
    long_opts = AssOptions(
        variant="landscape", safe_zones=safe_zones_for("landscape"), emphasis={0: "doorbell"}
    )
    for st in ("clean_white", "minimal_lower"):
        out[f"long_ass_{st}"] = build_ass(story_words, st, fonts_dir=empty_fonts, **long_opts.kwargs())

    # chapter cards
    for name, card in sorted(load_chapter_cards().items()):
        out[f"card_ass_{name}"] = longform.card_ass("The Night Shift Begins", 2, card, 3.0)

    # clips: graph and ASS per template
    templates = clip_layout.load_templates()
    for name in sorted(templates):
        tpl = templates[name]
        lay = rich_layout(name)
        start, end = 2.0, 9.0
        crops = clip_layout.plan_layout(name, SRC_W, SRC_H, lay, t_start=start, t_end=end)
        spec = clip_render.RenderSpec(
            video=tmp / "source.mp4",
            out=tmp / "clip.mp4",
            start=start,
            end=end,
            template=tpl,
            crops=crops,
            src_w=SRC_W,
            src_h=SRC_H,
            ass=tmp / "captions" / "c1.ass",
        )
        out[f"clip_graph_{name}"] = clip_render.build_filter_graph(spec)
        crop = crops[0]
        y_pct = clip_layout.caption_y_pct(tpl, crop)
        avoid = clip_layout.avoid_boxes(tpl, crop, lay, start, end)
        opts = AssOptions(
            variant="vertical",
            safe_zones=safe_zones_for("vertical"),
            avoid_regions=tuple(avoid),
            emphasis=emphasis_from_list(["", "truly"]),
            mask_profanity=False,
            anchor_y_pct=y_pct,
        )
        out[f"clip_ass_{name}"] = build_ass(
            clip_words(), "clean_white", fonts_dir=empty_fonts, **opts.kwargs()
        )

    # encoder arguments
    video_nv = VideoCfg(encoder="h264_nvenc", preset="p4", fps=30)
    video_cpu = VideoCfg(encoder="libx264", preset="ultrafast", fps=30)
    video_slow = VideoCfg(encoder="libx264", preset="medium", fps=30)
    lines = [
        "story nvenc p4: " + " ".join(render.encoder_args(video_nv)),
        "story nvenc forced cpu: " + " ".join(render.encoder_args(video_nv, force_cpu=True)),
        "story libx264 ultrafast: " + " ".join(render.encoder_args(video_cpu)),
        "story libx264 medium: " + " ".join(render.encoder_args(video_slow)),
        "clip nvenc p4: " + " ".join(clip_render.encoder_args("h264_nvenc", "p4")),
        "clip libx264 ultrafast: " + " ".join(clip_render.encoder_args("libx264", "ultrafast")),
        "clip libx264 medium: " + " ".join(clip_render.encoder_args("libx264", "medium")),
    ]
    out["encoder_args"] = "\n".join(lines) + "\n"
    return {k: normalise(v, tmp) for k, v in out.items()}


def main() -> None:
    """Write every case into this folder (run on the code the golden files are meant to pin)."""
    with tempfile.TemporaryDirectory(prefix="p10-golden-") as d:
        os.environ["NIGHTSHIFT_HOME"] = str(Path(d) / "home")  # no owner formats in the golden files
        built = cases(Path(d))
    for stale in GOLDEN_DIR.glob("*.txt"):
        stale.unlink()
    for stem, text in built.items():
        (GOLDEN_DIR / f"{stem}.txt").write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(built)} golden files to {GOLDEN_DIR}")


if __name__ == "__main__":
    sys.exit(main())
