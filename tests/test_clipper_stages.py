"""C8 Clipper: the clip stages on the synthetic fixture (fake services, real FFmpeg)."""
from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from ns_common import ffmpeg
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_studio import orchestrator
from ns_studio.clipper import stages_clip as sc
from ns_studio.clipper.models import ClipLayout, ClipMetadata, ClipParams, Transcript
from ns_studio.context import FAKEABLE, JobContext
from ns_studio.stages import REGISTRY, run_stage
from tests.fixtures.clipper.make_sample import facecam_truth, shared_sample
from tests.helpers import make_channel

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not available")
LLM_FIXTURES = Path(__file__).parent / "fixtures" / "clipper" / "llm"


def make_job(tmp_path: Path, **param_kw: object) -> JobContext:
    vod = tmp_path / "vod"
    vod.mkdir()
    shutil.copy2(shared_sample().video, vod / "sample.mp4")
    shutil.copy2(shared_sample().transcript, vod / "sample.transcript.json")
    params = ClipParams(source=str(vod / "sample.mp4"), channel="after-dark", rights_confirmed=True)
    params = replace(params, **param_kw)  # type: ignore[arg-type]
    return sc.create_clip_job(
        Paths.from_env(), make_channel(), params, fake=FAKEABLE, llm_fixtures=LLM_FIXTURES
    )


def run_through(ctx: JobContext, last: str) -> None:
    for st in sc.CLIP_STAGES:
        run_stage(ctx, st)
        if st.name == last:
            return


# ---------- registry ----------
def test_clip_format_is_registered() -> None:
    assert orchestrator.FORMAT_STAGES["clip"] == [
        "clip_import", "clip_audio", "clip_transcribe", "clip_select", "clip_detect", "clip_captions",
        "clip_render", "clip_seo", "clip_qc", "clip_outbox",
    ]
    stages = orchestrator.plan_job("clip")
    tenants = [s.tenant for s in stages]
    assert tenants == [None, None, "whisper", "llama", None, None, None, "llama", None, None]
    assert all(s.name in REGISTRY for s in stages)


def test_default_combo_uses_channel_pools(tmp_home: Path) -> None:
    ch = make_channel()
    assert sc.default_combo(ch).caption_style == ch.pools.caption_styles[0]


def test_place_captions_moves_margin_v() -> None:
    names = "Name,Fontname,Fontsize,P,S,O,B,Bold,I,U,St,ScX,ScY,Sp,An,BS,Out,Sh,Al,ML,MR,MV,Enc"
    style = "Style: Default,Arial,60,&H0,&H0,&H0,&H0,0,0,0,0,100,100,0,0,1,3,0,2,40,40,100,1"
    ass = f"[V4+ Styles]\nFormat: {names}\n{style}\n[Events]\n"
    out = sc.place_captions(ass, 50.0)
    line = next(ln for ln in out.split("\n") if ln.startswith("Style: Default"))
    assert line.split(",")[-2] == str(round(1920 * 0.5 - 0.6 * 60))
    assert line.split(",")[:-2] == style.split(",")[:-2] and line.endswith(",1")


def test_clip_sheet_text() -> None:
    meta = ClipMetadata(
        channel="after-dark", job_id="after-dark-20261101-ab12", k=1, title="T", description="D",
        tags=["a", "b"], hashtags=["#Shorts"], publish_after="2026-11-02T18:00", source_video="v.mp4",
        template="center_crop",
    )
    txt = sc.clip_sheet_text(meta, "c1.mp4")
    assert "TITLE: T" in txt and "TAGS: a, b" in txt and "Source video: v.mp4" in txt


# ---------- first five stages ----------
@needs_ffmpeg
def test_import_to_detect(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path)
    run_through(ctx, "clip_detect")
    info = ctx.read_json("source.json")
    assert info["source_id"].startswith("src-") and Path(info["path"]).exists()
    assert ffmpeg.probe(ctx.file("audio.wav"))["streams"][0]["sample_rate"] == "16000"
    tr = Transcript.model_validate(ctx.read_json("transcript.json"))
    assert len(tr.segments) == 6 and tr.segments[1].speaker == 1
    plan = ctx.read_json("clip_plan.json")
    assert [c["k"] for c in plan] == [1, 2]
    lay = ClipLayout.model_validate(ctx.read_json("clip_layout.json"))
    assert lay.template == "gaming_facecam" and lay.note is None
    truth = facecam_truth()
    assert lay.facecam is not None
    assert abs(lay.facecam.x - truth["x"]) <= 0.05 * 1280 and abs(lay.facecam.y - truth["y"]) <= 0.05 * 720
    assert lay.crops and all(c.region_b is not None for c in lay.crops)
    row = ctx.db.one("SELECT status, template FROM sources WHERE source_id=?", (info["source_id"],))
    assert row is not None and (row["status"], row["template"]) == ("detected", "gaming_facecam")


@needs_ffmpeg
def test_stages_are_idempotent(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path)
    run_through(ctx, "clip_detect")
    again = [run_stage(ctx, st) for st in sc.CLIP_STAGES[:5]]
    assert again == [False] * 5


@needs_ffmpeg
def test_reselect_does_not_retranscribe(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path)
    run_through(ctx, "clip_detect")
    replace(ClipParams.load(ctx.job_dir), max_clips=1).save(ctx.job_dir)
    ran = {st.name: run_stage(ctx, st) for st in sc.CLIP_STAGES[:5]}
    assert ran["clip_transcribe"] is False and ran["clip_audio"] is False
    assert ran["clip_select"] is True and ran["clip_detect"] is True
    assert len(ctx.read_json("clip_plan.json")) == 1
    assert len(ctx.read_json("clip_layout.json")["crops"]) >= 1


@needs_ffmpeg
def test_unconfirmed_rights_are_refused(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path, rights_confirmed=False)
    with pytest.raises(NsError) as ei:
        run_stage(ctx, sc.CLIP_STAGES[0])
    assert ei.value.code == "NS-E405"


@needs_ffmpeg
def test_requested_template_without_faces_falls_back_with_a_note(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path, template="podcast_stacked")
    run_through(ctx, "clip_detect")
    lay = ClipLayout.model_validate(ctx.read_json("clip_layout.json"))
    assert lay.template == "center_crop" and lay.note is not None and "NS-W402" in lay.note


# ---------- whole job (needs C6's captions, QC and outbox) ----------
@needs_ffmpeg
def test_full_clip_job(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path)
    ran = orchestrator.run_job(ctx)
    assert ran == orchestrator.FORMAT_STAGES["clip"]
    info = ctx.read_json("source.json")
    outbox = sorted(ctx.paths.outbox.glob(f"after-dark/*/clip-{info['source_id']}/c*.mp4"))
    assert len(outbox) >= 2
    for mp4 in outbox:
        assert ffmpeg.video_size(mp4) == (1080, 1920)
        meta = ClipMetadata.model_validate_json(mp4.with_suffix(".json").read_text(encoding="utf-8"))
        assert meta.kind == "clip" and meta.template == "gaming_facecam" and "#Shorts" in meta.hashtags
        assert mp4.with_suffix(".txt").exists()
    rows = ctx.db.query("SELECT kind, source_id FROM outputs WHERE job_id=?", (ctx.job_id,))
    assert len(rows) == len(outbox) and all(r["kind"] == "clip" for r in rows)
    assert all(r["source_id"] == info["source_id"] for r in rows)
    ass = ctx.file("captions/c1.ass").read_text(encoding="utf-8")
    assert "Dialogue:" in ass
    assert all(v["passed"] for v in ctx.read_json("qc/report.json").values())
