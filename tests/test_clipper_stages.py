"""C8 Clipper: the clip stages on the synthetic fixture (fake services, real FFmpeg)."""
from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from ns_common import ffmpeg
from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_common.paths import Paths
from ns_studio import orchestrator
from ns_studio.clipper import stages_clip as sc
from ns_studio.clipper.models import Analysis, ClipLayout, ClipMetadata, ClipParams, Transcript
from ns_studio.context import FAKEABLE, JobContext
from ns_studio.stages import REGISTRY, run_stage
from tests.fixtures.clipper.make_sample import build_transcript, facecam_truth, shared_sample
from tests.helpers import make_channel, make_ctx

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not available")
LLM_FIXTURES = Path(__file__).parent / "fixtures" / "clipper" / "llm"
UP_TO_LAYOUT = 6  # import, audio, analyze, transcribe, select, layout


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


# ---------- registry and pure helpers ----------
def test_clip_format_is_registered_in_the_revised_order() -> None:
    assert orchestrator.FORMAT_STAGES["clip"] == [
        "clip_import", "clip_audio", "clip_analyze", "clip_transcribe", "clip_select", "clip_layout",
        "clip_captions", "clip_render", "clip_seo", "clip_qc", "clip_outbox",
    ]
    stages = orchestrator.plan_job("clip")
    tenants = [s.tenant for s in stages]
    assert tenants == [None, None, None, "whisper", "llama", None, None, None, "llama", None, None]
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


def test_edge_sentences() -> None:
    tr = Transcript.model_validate(build_transcript())
    first, last = sc.edge_sentences([w for w in tr.words if w.seg == 0])
    assert first.startswith("Welcome back") and first.endswith("level.")
    assert last.startswith("Let us jump") and last.endswith("get.")
    assert sc.edge_sentences([]) == ("", "")


def test_local_safe_slug_removes_reserved_characters_and_names() -> None:
    assert sc._local_safe_slug('A: "title" <x>/y?*|') == "A title x y"
    assert sc._local_safe_slug("con") == "con_"
    assert sc._local_safe_slug("trailing dot. ") == "trailing dot"
    assert len(sc._local_safe_slug("x" * 200)) == 80


def test_safe_slug_is_windows_safe() -> None:
    out = sc.safe_slug('Raid: "night" 2?')
    assert out and not any(ch in out for ch in '<>:"/\\|?*') and len(out) <= 80


def test_clip_file_stem_is_numbered_and_short() -> None:
    stem = sc.clip_file_stem(3, 'Why: "this" works? ' * 10)
    assert stem.startswith("clip-03 ") and len(stem) <= 80
    assert not any(ch in stem for ch in '<>:"/\\|?*')


def test_clip_folder_collisions_get_a_suffix(tmp_home: Path) -> None:
    ctx = make_ctx(tmp_home, fmt="clip")
    first = sc.clip_folder(ctx, "src-aaa", "Raid Night.mp4")
    assert first == ctx.paths.outbox / "after-dark" / "clips" / "Raid Night"
    ctx.db.execute(
        "INSERT INTO sources(source_id, path, imported_at) VALUES ('src-bbb','p','2026-11-01T00:00:00')"
    )
    ctx.db.execute(
        "INSERT INTO outputs(output_id, job_id, channel, kind, title, path, status, source_id, created_at)"
        " VALUES ('j/c1','j','after-dark','clip','t',?,'pending','src-bbb','2026-11-01T00:00:00')",
        (str(first / "clip-01 t.mp4"),),
    )
    assert sc.clip_folder(ctx, "src-aaa", "Raid Night.mp4").name == "Raid Night-2"
    assert sc.clip_folder(ctx, "src-bbb", "Raid Night.mp4") == first


# ---------- the stages up to layout ----------
@needs_ffmpeg
def test_import_to_layout(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path)
    run_through(ctx, "clip_layout")
    info = ctx.read_json("source.json")
    assert info["source_id"].startswith("src-") and Path(info["path"]).exists()
    assert ffmpeg.probe(ctx.file("audio.wav"))["streams"][0]["sample_rate"] == "16000"
    an = Analysis.model_validate(ctx.read_json("analysis.json"))
    assert (an.width, an.height) == (1280, 720) and an.faces_detected is False
    truth = facecam_truth()
    assert an.facecam is not None
    assert abs(an.facecam.x - truth["x"]) <= 0.05 * 1280 and abs(an.facecam.y - truth["y"]) <= 0.05 * 720
    assert an.loudness.integrated_lufs is not None
    tr = Transcript.model_validate(ctx.read_json("transcript.json"))
    assert len(tr.segments) == 6 and tr.segments[1].speaker == 1
    plan = ctx.read_json("clip_plan.json")
    kept = [c for c in plan if c["status"] == "kept"]
    assert [c["k"] for c in kept] == [1, 2] and kept[1]["layout"] == "clip_podcast_stacked"
    assert any(c["status"] == "dropped" and c["reasons"] for c in plan)
    lay = ClipLayout.model_validate(ctx.read_json("clip_layout.json"))
    assert lay.per_clip == {"1": "gaming_facecam", "2": "gaming_facecam"}
    assert "2" in lay.notes and "NS-W402" in lay.notes["2"] and "1" not in lay.notes
    assert lay.crops and all(c.region_b is not None for c in lay.crops)
    row = ctx.db.one("SELECT status, template FROM sources WHERE source_id=?", (info["source_id"],))
    assert row is not None and (row["status"], row["template"]) == ("laid_out", "gaming_facecam")


@needs_ffmpeg
def test_stages_are_idempotent(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path)
    run_through(ctx, "clip_layout")
    again = [run_stage(ctx, st) for st in sc.CLIP_STAGES[:UP_TO_LAYOUT]]
    assert again == [False] * UP_TO_LAYOUT


@needs_ffmpeg
def test_reselect_does_not_reanalyze_or_retranscribe(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path)
    run_through(ctx, "clip_layout")
    replace(ClipParams.load(ctx.job_dir), max_clips=1).save(ctx.job_dir)
    ran = {st.name: run_stage(ctx, st) for st in sc.CLIP_STAGES[:UP_TO_LAYOUT]}
    assert ran["clip_audio"] is False and ran["clip_analyze"] is False and ran["clip_transcribe"] is False
    assert ran["clip_select"] is True and ran["clip_layout"] is True
    kept = [c for c in ctx.read_json("clip_plan.json") if c["status"] == "kept"]
    assert len(kept) == 1


@needs_ffmpeg
def test_judge_failures_are_recorded_and_the_next_candidate_is_used(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path)
    answers = [{"ok": False, "reasons": ["ends before the payoff"]}, {"ok": True, "reasons": []}]
    ctx.services.llm = FakeLlm(LLM_FIXTURES, overrides={"clip_judge": answers})
    run_through(ctx, "clip_select")
    plan = ctx.read_json("clip_plan.json")
    kept = [c for c in plan if c["status"] == "kept"]
    dropped = [c for c in plan if c["status"] == "dropped"]
    assert [c["title"] for c in kept] == ["Beat the boss with four laps"] and kept[0]["k"] == 1
    assert any(c["reasons"] == ["ends before the payoff"] for c in dropped)
    assert ctx.prompt_versions.get("clip_judge") is not None


@needs_ffmpeg
def test_all_rejected_is_e407_and_the_plan_keeps_the_reasons(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path)
    no = {"ok": False, "reasons": ["not standalone"]}
    ctx.services.llm = FakeLlm(LLM_FIXTURES, overrides={"clip_judge": no})
    with pytest.raises(NsError) as ei:
        run_through(ctx, "clip_select")
    assert ei.value.code == "NS-E407" and "not standalone" in ei.value.detail
    plan = ctx.read_json("clip_plan.json")
    assert plan and all(c["status"] == "dropped" for c in plan)


@needs_ffmpeg
def test_unconfirmed_rights_are_refused(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path, rights_confirmed=False)
    with pytest.raises(NsError) as ei:
        run_stage(ctx, sc.CLIP_STAGES[0])
    assert ei.value.code == "NS-E405"


@needs_ffmpeg
def test_explicit_template_without_faces_falls_back_to_center_crop(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path, template="podcast_stacked")
    run_through(ctx, "clip_layout")
    lay = ClipLayout.model_validate(ctx.read_json("clip_layout.json"))
    assert set(lay.per_clip.values()) == {"center_crop"}
    assert lay.note is not None and "NS-W402" in lay.note and set(lay.notes) == {"1", "2"}


# ---------- whole job ----------
@needs_ffmpeg
def test_full_clip_job(tmp_home: Path, tmp_path: Path) -> None:
    ctx = make_job(tmp_path)
    ran = orchestrator.run_job(ctx)
    assert ran == orchestrator.FORMAT_STAGES["clip"]
    info = ctx.read_json("source.json")
    folder = ctx.paths.outbox / "after-dark" / "clips" / "sample"
    outbox = sorted(folder.glob("clip-*.mp4"))
    assert [p.name[:8] for p in outbox] == ["clip-01 ", "clip-02 "]
    for mp4 in outbox:
        assert ffmpeg.video_size(mp4) == (1080, 1920)
        meta = ClipMetadata.model_validate_json(mp4.with_suffix(".json").read_text(encoding="utf-8"))
        assert meta.kind == "clip" and meta.template == "gaming_facecam" and "#Shorts" in meta.hashtags
        assert mp4.with_suffix(".txt").exists()
    rows = ctx.db.query("SELECT kind, source_id, path FROM outputs WHERE job_id=?", (ctx.job_id,))
    assert len(rows) == len(outbox) and all(r["kind"] == "clip" for r in rows)
    assert all(r["source_id"] == info["source_id"] for r in rows)
    assert {Path(r["path"]) for r in rows} == set(outbox)
    ass = ctx.file("captions/c1.ass").read_text(encoding="utf-8")
    assert "Dialogue:" in ass
    assert all(v["passed"] for v in ctx.read_json("qc/report.json").values())
