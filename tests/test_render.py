from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.models import VideoCfg
from ns_studio.media.audio import measure_lufs
from ns_studio.media.captions import build_ass
from ns_studio.media.presets import load_visual_treatments, visual_treatment
from ns_studio.media.render import (
    build_landscape_graph,
    build_short_audio,
    build_vertical_graph,
    encoder_args,
    escape_filter_path,
    post_chain,
    render_video,
    zoom_filter,
)
from ns_studio.media.shotplan import Shot, frame_edges
from ns_studio.media.transcribe import Word

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")


def shot(i: int, path: str, w: int, h: int, start: float, end: float, ss: float = 0.0) -> Shot:
    return Shot(0, start, end, "fake", f"c{i}", path, ss, w, h, "u", "l", "kw")


def _shots_for_graph() -> list[Shot]:
    return [
        shot(0, "a.mp4", 1920, 1080, 0.0, 2.0),
        shot(1, "b.mp4", 1080, 1920, 2.0, 4.0),
        shot(2, "c.mp4", 1920, 1080, 4.0, 6.0),
    ]


def test_escape_filter_path_quotes_and_escapes_drive_colon():
    assert escape_filter_path(Path("C:/Users/me/cap.ass")) == "'C\\:/Users/me/cap.ass'"
    # an apostrophe is escaped, not deleted: close quote, three backslashes + quote, reopen
    assert escape_filter_path(Path("C:/it's/cap.ass")) == "'C\\:/it'" + "\\\\\\'" + "'s/cap.ass'"


def test_vertical_graph_structure():
    shots = _shots_for_graph()
    edges = frame_edges([s.end for s in shots], 30)
    g = build_vertical_graph(shots, edges, 30, visual_treatment("dark_slowzoom"), Path("C:/x/cap.ass"))
    assert g.count("concat=n=3:v=1:a=0[cat]") == 1
    assert g.count("boxblur") == 2  # only the two wide clips get the blurred fill
    assert g.count("zoompan=") == 3
    assert "trim=end_frame=60" in g and "scale=1080:1920" in g and "crop=w=ih*4/5:h=ih" in g
    assert "eq=brightness=-0.06" in g and "curves=preset=darker" in g
    assert "vignette=angle=PI/4" in g and "noise=alls=10:allf=t+u" in g
    assert "ass=filename='C\\:/x/cap.ass'" in g
    assert g.endswith("format=yuv420p[vout]")
    for i in range(3):
        assert f"[v{i}]" in g


def test_landscape_graph_uses_1920x1080_and_never_blurs():
    shots = [shot(0, "a.mp4", 1920, 1080, 0.0, 6.0)]
    g = build_landscape_graph(shots, [0, 180], 30, visual_treatment("clean_bright"), None)
    assert "scale=1920:1080" in g and "boxblur" not in g and "ass=" not in g


def test_zoom_filters_per_mode():
    t = load_visual_treatments()
    assert "min(zoom+0.0007,1.12)" in (zoom_filter(t["dark_slowzoom"].zoom, 1080, 1920, 30, 0) or "")
    assert "max(1.08-on*0.0005,1)" in (zoom_filter(t["clean_bright"].zoom, 1080, 1920, 30, 0) or "")
    hand = zoom_filter(t["handheld_grit"].zoom, 1080, 1920, 30, 2) or ""
    assert "z=1.07" in hand and "sin(on/9+3.4)" in hand
    assert zoom_filter(t["cinematic_teal"].zoom, 1080, 1920, 30, 0) is None


def test_post_chain_for_teal_has_colorbalance_and_no_grain_zero():
    chain = post_chain(visual_treatment("cinematic_teal"), None, None)
    assert "colorbalance=rs=-0.08" in chain and "noise=alls=6" in chain and chain.endswith("format=yuv420p")
    assert "noise" not in post_chain(visual_treatment("clean_bright"), None, None)


def test_encoder_args():
    nv = encoder_args(VideoCfg(encoder="h264_nvenc", preset="p4"))
    assert nv[:4] == ["-c:v", "h264_nvenc", "-preset", "p4"]
    cpu = encoder_args(VideoCfg(encoder="libx264", preset="ultrafast"))
    assert cpu[:4] == ["-c:v", "libx264", "-preset", "veryfast"]      # ultrafast made 900 MB Shorts
    forced = encoder_args(VideoCfg(encoder="h264_nvenc", preset="p4"), force_cpu=True)
    assert forced[1] == "libx264" and forced[3] == "veryfast"
    for args in (nv, cpu, forced):                                        # every render is bitrate-capped
        assert args[args.index("-maxrate") + 1] == "12M" and "-bufsize" in args


def test_render_without_shots_is_e401(tmp_path: Path):
    with pytest.raises(NsError) as ei:
        render_video(shots=[], audio=tmp_path / "a.wav", out=tmp_path / "o.mp4", size=(1080, 1920),
                     treatment=visual_treatment("dark_slowzoom"), video=VideoCfg(encoder="libx264"),
                     ass_path=None)
    assert ei.value.code == "NS-E401"


def _make_inputs(tmp_path: Path, seconds: float):
    wide = ff.make_test_clip(tmp_path / "wide.mp4", width=320, height=180, seconds=3.0)
    tall = ff.make_test_clip(tmp_path / "tall.mp4", width=180, height=320, seconds=3.0, kind="color",
                             color="0x406080")
    audio = ff.make_tone_wav(tmp_path / "a.wav", seconds, sr=48000)
    toks = "it was a quiet night".split()
    words = [Word(t, i * 0.3, i * 0.3 + 0.25) for i, t in enumerate(toks)]
    ass = tmp_path / "cap.ass"
    ass.write_text(build_ass(words, "punch_yellow"), encoding="utf-8")
    return wide, tall, audio, ass


@needs_ffmpeg
@pytest.mark.parametrize("treatment", ["dark_slowzoom", "handheld_grit", "clean_bright", "cinematic_teal"])
def test_render_video_every_treatment(tmp_path: Path, treatment: str):
    wide, tall, audio, ass = _make_inputs(tmp_path, 1.6)
    shots = [
        shot(0, str(wide), 320, 180, 0.0, 0.8, ss=0.2),
        shot(1, str(tall), 180, 320, 0.8, 1.6, ss=0.0),
    ]
    out = render_video(
        shots=shots, audio=audio, out=tmp_path / f"{treatment}.mp4", size=(1080, 1920),
        treatment=visual_treatment(treatment), video=VideoCfg(encoder="libx264", preset="ultrafast"),
        ass_path=ass,
    )
    assert ff.video_size(out) == (1080, 1920)
    assert abs(ff.duration_s(out) - 1.6) < 0.15
    kinds = sorted(s["codec_type"] for s in ff.probe(out)["streams"])
    assert kinds == ["audio", "video"] and ff.has_faststart(out)
    vid = next(s for s in ff.probe(out)["streams"] if s["codec_type"] == "video")
    assert vid["codec_name"] == "h264" and vid["r_frame_rate"] == "30/1"


@needs_ffmpeg
def test_render_falls_back_to_libx264_when_nvenc_fails(tmp_path: Path):
    wide, _tall, audio, ass = _make_inputs(tmp_path, 1.0)
    out = render_video(
        shots=[shot(0, str(wide), 320, 180, 0.0, 1.0)], audio=audio, out=tmp_path / "o.mp4",
        size=(1080, 1920), treatment=visual_treatment("clean_bright"),
        video=VideoCfg(encoder="h264_nvenc", preset="p4"), ass_path=ass,
    )
    assert ff.video_size(out) == (1080, 1920)


@needs_ffmpeg
def test_render_short_clip_is_padded_not_truncated(tmp_path: Path):
    short = ff.make_test_clip(tmp_path / "short.mp4", width=320, height=180, seconds=0.5)
    audio = ff.make_tone_wav(tmp_path / "a.wav", 1.5, sr=48000)
    out = render_video(
        shots=[shot(0, str(short), 320, 180, 0.0, 1.5)], audio=audio, out=tmp_path / "o.mp4",
        size=(1080, 1920), treatment=visual_treatment("cinematic_teal"),
        video=VideoCfg(encoder="libx264", preset="ultrafast"), ass_path=None,
    )
    assert abs(ff.duration_s(out) - 1.5) < 0.15


@needs_ffmpeg
def test_build_short_audio_orders_hook_gap_body_and_normalizes(tmp_path: Path):
    hook = ff.make_tone_wav(tmp_path / "hook.wav", 1.0, sr=48000, freq=880.0)
    body = ff.make_tone_wav(tmp_path / "body.wav", 4.0, sr=48000)
    out = build_short_audio(hook, body, tmp_path / "raw.wav", tmp_path / "final.wav", 0.15)
    assert abs(ff.duration_s(out) - 5.15) < 0.05
    lufs, tp = measure_lufs(out)
    assert abs(lufs - (-14.0)) <= 0.7 and tp <= -1.0
    only_body = build_short_audio(None, body, tmp_path / "raw2.wav", tmp_path / "final2.wav", 0.15)
    assert abs(ff.duration_s(only_body) - 4.0) < 0.05


def test_filter_path_keeps_an_apostrophe_in_a_windows_home():
    """Review row 14: C:/Users/O'Brien/... lost its apostrophe and every render failed with NS-E401."""
    p = escape_filter_path(Path("C:/Users/O'Brien/Nightshift/cap.ass"))
    assert "Brien" in p and "O'" in p and "OBrien" not in p
    from ns_studio.clipper.render import filter_path

    assert filter_path(Path("C:/it's/c1.ass")) == escape_filter_path(Path("C:/it's/c1.ass"))[1:-1]


@pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
def test_ffmpeg_reads_an_ass_file_from_a_folder_with_an_apostrophe(tmp_path: Path):
    folder = tmp_path / "O'Brien"
    folder.mkdir()
    ass = folder / "cap.ass"
    words = [Word("Hello", 0.0, 0.2), Word("there", 0.2, 0.4)]
    ass.write_text(build_ass(words, "clean_white"), encoding="utf-8")
    graph = f"ass=filename={escape_filter_path(ass)}"
    ff.run(["-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.5", "-vf", graph, "-f", "null", "-"])
