"""H-6: setup asks for the space this install will really use (console wizard and setup window)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from installer.stage2 import sysinfo
from installer.stage2.steps.s01_questions import S01Questions
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.web import answers_view, plan, preflight
from ns_common import diskneed
from ns_common.diskneed import GIB
from ns_common.errors import NsError
from tests.test_diskneed import ROOT, place_models, place_programs_and_venvs, put_verified

D = "D:\\Nightshift"


@pytest.fixture(scope="module")
def raw() -> dict[str, Any]:
    return plan.load_manifest(ROOT)


def provided(tmp_path: Path, **over: Any) -> dict[str, Any]:
    return example_answers(home_path=str(tmp_path / "home"), **over)


# ------------------------------------------------------------------------------------- the console wizard
def test_s01_refusal_names_the_number_what_it_is_made_of_and_what_is_free(tmp_path):
    ctx = make_test_context(tmp_path, raw_answers=provided(tmp_path), ui=FakeUi(interactive=False))
    ctx.free_gb_fn = lambda _p: 49.0
    with pytest.raises(NsError) as ei:
        S01Questions().run(ctx)
    e = ei.value
    assert e.code == "NS-E104"
    assert e.detail.startswith("This install needs about 60 GB (")
    assert "models to download" in e.detail and "working reserve for videos" in e.detail
    assert e.detail.endswith("has 49 GB free")
    assert "\n" not in e.detail
    assert "60 GB" not in e.definition.hint


def test_s01_does_not_refuse_a_home_whose_models_are_already_downloaded(tmp_path):
    """49 GB free is not enough for a first install (60) but is for a rerun over downloaded models."""
    ctx = make_test_context(tmp_path, raw_answers=provided(tmp_path), ui=FakeUi(interactive=False))
    ctx.free_gb_fn = lambda _p: 49.0
    place_models(plan.load_manifest(ROOT), tmp_path / "home")
    S01Questions().run(ctx)
    assert ctx.answers is not None


def test_s01_a_finished_install_is_only_asked_for_its_headroom(tmp_path):
    ctx = make_test_context(tmp_path, raw_answers=provided(tmp_path), ui=FakeUi(interactive=False))
    ctx.free_gb_fn = lambda _p: 3.0
    place_models(plan.load_manifest(ROOT), tmp_path / "home")
    place_programs_and_venvs(tmp_path / "home")
    S01Questions().run(ctx)
    assert ctx.answers is not None
    ctx2 = make_test_context(tmp_path, raw_answers=provided(tmp_path), ui=FakeUi(interactive=False))
    ctx2.free_gb_fn = lambda _p: 1.0
    with pytest.raises(NsError) as ei:
        S01Questions().run(ctx2)
    assert ei.value.code == "NS-E104" and "no models to download" in ei.value.detail


def test_s01_a_small_custom_model_lowers_the_number(tmp_path):
    custom = {"repo": "someone/tiny-gguf", "file": "tiny-1b-q4.gguf", "size": 1_000_000_000}
    ctx = make_test_context(
        tmp_path, raw_answers=provided(tmp_path, llm_custom=custom), ui=FakeUi(interactive=False)
    )
    ctx.free_gb_fn = lambda _p: 55.0
    S01Questions().run(ctx)  # 60 GB with the 9 GB model; 52 with a 1 GB one
    assert ctx.answers is not None and ctx.answers.llm_custom is not None


def test_s01_the_engine_test_shape_passes_where_the_full_install_does_not(tmp_path):
    """Fake voice on a PC with no NVIDIA card (cpu-dev): about 2 GB of files plus the reserve."""
    from installer.stage2.sysinfo import sample_preflight

    pf = sample_preflight(tmp_path / "home", ROOT)
    pf.update(profile="cpu-dev", llama_pair="", cuda="")
    raw_a = provided(tmp_path, fake_voice=True, allow_cpu=True, fake_footage=True)
    ctx = make_test_context(tmp_path, raw_answers=raw_a, ui=FakeUi(interactive=False), preflight=pf)
    ctx.free_gb_fn = lambda _p: 45.0
    S01Questions().run(ctx)
    assert ctx.answers is not None
    # the same PC with a voice engine asks for more than 45 GB
    other = tmp_path / "b"
    pf2 = sample_preflight(other / "home", ROOT)
    pf2.update(profile="cpu-dev", llama_pair="", cuda="")
    ctx2 = make_test_context(
        other, raw_answers=provided(other, allow_cpu=True), ui=FakeUi(interactive=False), preflight=pf2
    )
    ctx2.free_gb_fn = lambda _p: 41.0
    with pytest.raises(NsError) as ei:
        S01Questions().run(ctx2)
    assert ei.value.code == "NS-E104"


def test_s01_unknown_free_space_does_not_block_and_is_logged(tmp_path):
    ctx = make_test_context(tmp_path, raw_answers=provided(tmp_path), ui=FakeUi(interactive=False))

    def unreadable(_p: Path) -> float:
        raise OSError("the volume does not answer")

    ctx.free_gb_fn = unreadable
    S01Questions().run(ctx)
    assert ctx.answers is not None
    log = "\n".join(f.read_text(encoding="utf-8") for f in ctx.paths.logs.glob("*.jsonl"))
    assert "disk_free_unknown" in log


def test_s01_a_dev_install_is_still_exempt(tmp_path):
    ctx = make_test_context(
        tmp_path, raw_answers=provided(tmp_path), ui=FakeUi(interactive=False), dev_mode=True
    )
    ctx.free_gb_fn = lambda _p: 12.0
    S01Questions().run(ctx)
    assert ctx.answers is not None
    ctx2 = make_test_context(
        tmp_path / "b", raw_answers=provided(tmp_path / "b"), ui=FakeUi(interactive=False), dev_mode=True
    )
    ctx2.free_gb_fn = lambda _p: 8.0
    with pytest.raises(NsError) as ei:
        S01Questions().run(ctx2)
    assert ei.value.code == "NS-E104" and "10 GB" in ei.value.detail


def test_sysinfo_asks_diskneed_and_no_longer_holds_the_fixed_figures():
    assert not hasattr(sysinfo, "TIER_EXTRA_GB") and not hasattr(sysinfo, "BASE_FREE_GB")
    small, balanced, best = (sysinfo.required_free_gb(t) for t in ("small", "balanced", "best"))
    assert 59 <= small <= 61 and small < balanced < best
    assert sysinfo.required_free_gb("small", 1_000_000_000) < small - 7


# ----------------------------------------------------------------------------------------- the window
def test_the_window_plan_carries_the_computed_number_and_its_makeup(raw):
    p = plan.build_plan(
        raw, profile="gpu", tier="small", include_code=True, cuda="13.0", ram_gb=32, vram_mb=16000
    )
    assert 59 <= p["needFreeGb"] <= 61
    assert p["needFreeGb"] == plan.required_free_gb("small")
    assert "working reserve for videos" in p["needMakeup"] and "\n" not in p["needMakeup"]
    assert p["tiers"]["small"]["needFreeGb"] == p["needFreeGb"]
    assert p["tiers"]["balanced"]["needFreeGb"] > p["tiers"]["small"]["needFreeGb"]


def test_the_window_plan_for_a_home_with_models_asks_for_less(raw, tmp_path):
    place_models(raw, tmp_path / "h")
    full = plan.build_plan(
        raw, profile="gpu", tier="small", include_code=True, cuda="13.0", ram_gb=32, vram_mb=16000
    )
    here = plan.build_plan(
        raw,
        profile="gpu",
        tier="small",
        include_code=True,
        cuda="13.0",
        ram_gb=32,
        vram_mb=16000,
        home=tmp_path / "h",
    )
    assert here["needFreeGb"] < full["needFreeGb"] - 13


def test_the_window_custom_model_and_fake_voice_change_the_number(raw):
    custom = {"repo": "a/b", "file": "tiny-1b-q4.gguf", "size": 1_000_000_000}
    base = plan.required_free_gb("small")
    assert plan.required_free_gb("small", 1_000_000_000) < base - 7
    p = plan.build_plan(
        raw,
        profile="gpu",
        tier="small",
        include_code=True,
        cuda="13.0",
        ram_gb=32,
        vram_mb=16000,
        custom=custom,
    )
    assert p["needFreeGb"] == plan.required_free_gb("small", 1_000_000_000)
    quiet = plan.build_plan(
        raw,
        profile="gpu",
        tier="small",
        include_code=True,
        cuda="13.0",
        ram_gb=32,
        vram_mb=16000,
        voice=False,
    )
    assert quiet["needFreeGb"] < base - 4


def pf_with(free_gb: float, **kw: Any) -> dict[str, Any]:
    pf: dict[str, Any] = {
        "drives": [{"letter": "D", "free_gb": free_gb, "type": "SSD"}],
        "checks": [],
        "profile": "gpu",
        "llama_pair": "cuda13",
    }
    pf.update(kw)
    return pf


def test_the_pc_check_row_fails_under_the_computed_need_and_says_it_in_one_line():
    rows = preflight.raw_disk_rows(pf_with(49.0), D)
    disk = rows[0]
    assert disk["status"] == "fail" and disk["code"] == "NS-E104"
    assert disk["value"].startswith("This install needs about 60 GB (") and disk["value"].endswith(
        "D: has 49 GB free"
    )
    assert "\n" not in disk["value"]
    ok = preflight.raw_disk_rows(pf_with(61.0), D)[0]
    assert ok["status"] == "ok" and "D: has 61 GB free" in ok["value"] and "needs about 60 GB" in ok["value"]


def test_the_pc_check_row_follows_the_home_and_the_models_already_there(raw, tmp_path):
    home = tmp_path / "home"
    place_models(raw, home)
    need = plan.home_need(raw, str(home), "small")
    row = preflight.raw_disk_rows(pf_with(49.0), str(home), need=need)[0]
    assert row["status"] == "ok"  # 49 GB free is enough once the models are there
    rows = preflight.check_rows(pf_with(49.0), str(home), need=need)
    assert next(r for r in rows if r["id"] == "disk")["status"] == "ok"


def test_the_pc_check_row_for_a_dev_install_keeps_ten_gb():
    pf = pf_with(12.0, dev_mode=True, profile="cpu-dev")
    row = preflight.raw_disk_rows(pf, D)[0]
    assert row["status"] == "ok"
    assert preflight.raw_disk_rows(pf_with(8.0, dev_mode=True), D)[0]["status"] == "fail"


def test_the_warn_line_of_the_old_60_gb_recommendation_is_gone():
    row = preflight.raw_disk_rows(pf_with(55.0), D)[0]
    assert "recommended" not in row["value"]


def test_the_meter_and_the_folder_check_use_the_same_number(raw):
    m = preflight.machine_summary(pf_with(500.0), D, "balanced")
    assert m["meters"]["disk"]["needGb"] == plan.required_free_gb("balanced")
    drives = [{"letter": "D", "freeGb": 49.0, "type": "SSD"}]
    low = preflight.disk_check(D, "small", drives, lambda _p: 49.0)
    assert not low["ok"] and low["needGb"] == plan.required_free_gb("small")
    assert low["message"].startswith("Only 49 GB free on D:. This install needs about 60 GB (")
    assert "\n" not in low["message"]
    fine = preflight.disk_check(D, "small", [{"letter": "D", "freeGb": 70.0, "type": "SSD"}], lambda _p: 70.0)
    assert fine["ok"]


def test_the_folder_check_knows_a_home_with_models_in_it(raw, tmp_path):
    home = tmp_path / "home"
    place_models(raw, home)
    letter = (home.drive[:1] or "C").upper()
    drives = [{"letter": letter, "freeGb": 49.0, "type": "SSD"}]
    r = preflight.disk_check(str(home), "small", drives, lambda _p: 49.0)
    assert r["ok"] and r["needGb"] < 50


def test_the_answers_check_says_the_computed_number():
    env = answers_view.Env(ram_gb=32, tiers={}, drive_letters={"D"}, free_gb=lambda _p: 31.0)
    errs = answers_view._home_errors(D, "small", env)
    assert errs and errs[0]["code"] == "NS-E104"
    assert (
        errs[0]["message"]
        == f"Pick a folder on a drive with {plan.required_free_gb('small')} GB free. This one has 31 GB"
    )


def test_a_stale_stub_or_unmarked_file_in_a_home_does_not_count(raw, tmp_path):
    home = tmp_path / "home"
    llm = raw["profiles"]["gpu"]["llm"]
    dest = home / "models" / "llm" / Path(llm["file"]).name
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"half")  # no .ok mark: setup would download it
    assert (
        plan.home_need(raw, str(home), "small").total
        == plan.home_need(raw, str(tmp_path / "other"), "small").total
    )
    put_verified(dest, llm["sha256"])
    assert (
        plan.home_need(raw, str(home), "small").total
        < plan.home_need(raw, str(tmp_path / "other"), "small").total
    )
    assert diskneed.GIB == GIB


def test_stage1_ranks_drives_by_the_number_stage_2_computes():
    """Stage 1 (PowerShell) cannot work out the need: its one figure only ranks drives and follows stage 2."""
    text = (ROOT / "installer" / "stage1.ps1").read_text(encoding="utf-8")
    roomy = re.search(r"\$script:RoomyGb = (\d+)", text)
    assert roomy is not None and int(roomy.group(1)) == sysinfo.required_free_gb("small")
    assert "MinFreeGbRecommended" not in text and "60 GB recommended" not in text
    assert "Free disk space, 60 GB" not in text
