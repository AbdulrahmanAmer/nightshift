"""C1: the setup window's install plan equals what the wizard's downloader plans (real manifest.json)."""

from pathlib import Path

import pytest

from installer.stage2.downloader import plan_bins, plan_models, total_bytes
from installer.stage2.sysinfo import required_free_gb
from installer.stage2.web import plan
from ns_common.config import load_manifest

ROOT = Path(__file__).resolve().parents[1]
HOME = Path("D:\\ns-test-home")
CASES = [("gpu", "small"), ("gpu", "balanced"), ("gpu", "best"), ("cpu-dev", "small")]


@pytest.fixture(scope="module")
def manifest():
    return load_manifest(ROOT / "manifest.json")


@pytest.fixture(scope="module")
def raw():
    return plan.load_manifest(ROOT)


@pytest.mark.parametrize(("profile", "tier"), CASES)
@pytest.mark.parametrize("pair", ["cuda13", "cuda12"])
def test_byte_totals_equal_the_downloader_plan(manifest, raw, profile, tier, pair):
    pair = None if profile == "cpu-dev" else pair
    models = plan_models(manifest, profile, tier, HOME)
    bins = plan_bins(manifest, profile, HOME, include_code=True, llama_pair=pair)
    sizes = plan.planned_sizes(raw, profile, tier, include_code=True, llama_pair=pair)
    assert sum(s or 0 for s in sizes.values()) == total_bytes(models) + total_bytes(bins)
    # every sized model file is in the web plan under the same key
    for item in models:
        if item.size:
            assert sizes[item.name] == item.size, item.name
    # every program the downloader fetches is a row, and no extra row exists
    programs = {k for k in sizes if k.startswith(("llama", "cudart", "opencode", "yt_dlp", "webview2"))}
    assert {i.name for i in bins} == programs


def test_the_whisper_files_collapse_into_one_row_and_yunet_keeps_its_key(manifest, raw):
    rows = plan.model_rows(raw, "gpu", "small")
    keys = [r["key"] for r in rows]
    assert "whisper" in keys and "yunet" in keys
    assert not any(k.startswith("whisper/") for k in keys)
    names = {i.name for i in plan_models(manifest, "gpu", "small", HOME)}
    assert "yunet" in names and any(n.startswith("whisper/") for n in names)


def test_the_llm_row_follows_the_tier(raw):
    labels = {t: plan.model_rows(raw, "gpu", t)[0]["label"] for t in ("small", "balanced", "best")}
    assert labels == {"small": "Story model (Small)", "balanced": "Story model (Balanced)",
                      "best": "Story model (Best)"}
    sizes = {t: plan.model_rows(raw, "gpu", t)[0]["bytes"] for t in ("small", "balanced", "best")}
    assert sizes["small"] < sizes["balanced"] < sizes["best"]


def test_build_plan_groups_and_totals(raw):
    p = plan.build_plan(raw, profile="gpu", tier="small", include_code=True, cuda="13.0", ram_gb=32.0,
                        vram_mb=16311)
    assert [g["id"] for g in p["groups"]] == ["tools", "python", "programs", "models"]
    assert p["totalBytes"] == sum(i["bytes"] or 0 for g in p["groups"] for i in g["items"])
    assert p["needFreeGb"] == 60 and p["tier"] == "small" and p["profile"] == "gpu"
    assert p["unknownCount"] > 0                    # programs and whisper learn their size later
    keys = [i["key"] for g in p["groups"] for i in g["items"]]
    assert "llama_cuda13" in keys and "cudart13" in keys and "opencode" in keys
    no_code = plan.build_plan(raw, profile="gpu", tier="small", include_code=False, cuda="12.8", ram_gb=32.0,
                              vram_mb=16311)
    keys = [i["key"] for g in no_code["groups"] for i in g["items"]]
    assert "opencode" not in keys and "llama_cuda" in keys and "llama_cuda13" not in keys
    cpu = plan.build_plan(raw, profile="cpu-dev", tier="small", include_code=True, cuda=None, ram_gb=16.0,
                          vram_mb=None)
    assert "llama_cpu" in [i["key"] for g in cpu["groups"] for i in g["items"]]
    assert cpu["totalBytes"] < p["totalBytes"]


@pytest.mark.parametrize("tier", ["small", "balanced", "best"])
def test_required_free_space_matches_the_wizard(tier):
    assert plan.required_free_gb(tier) == required_free_gb(tier)


def test_tier_infos_say_why_a_tier_is_not_allowed(raw):
    ok = plan.tier_infos(raw, "gpu", ram_gb=32.0, vram_mb=16311)
    assert all(t["allowed"] for t in ok.values()) and ok["best"]["llmBytes"] > ok["balanced"]["llmBytes"]
    low_ram = plan.tier_infos(raw, "gpu", ram_gb=16.0, vram_mb=16311)
    assert low_ram["small"]["allowed"] and low_ram["balanced"]["reason"] == "Needs 32 GB of memory"
    low_vram = plan.tier_infos(raw, "gpu", ram_gb=32.0, vram_mb=8192)
    assert low_vram["best"]["reason"] == "Needs a 16 GB graphics card"
    cpu = plan.tier_infos(raw, "cpu-dev", ram_gb=32.0, vram_mb=None)
    assert not cpu["balanced"]["allowed"] and cpu["small"]["allowed"]


@pytest.mark.parametrize(
    ("total", "label", "download_min"),
    [(0, "About 15 minutes", 0), (1_000_000_000, "About 20 minutes", 3), (17_300_000_000, "About 1 hour", 47),
     (100_000_000_000, "About 4.5 hours", 267)],
)
def test_time_estimate_follows_the_spec_rounding(total, label, download_min):
    est = plan.estimate(total)
    assert est["label"] == label and est["downloadMin"] == download_min and est["installMin"] == 15


def test_pinned_and_llama_pair_helpers():
    assert plan.pinned("a" * 64) and not plan.pinned("TBD-release") and not plan.pinned(None)
    assert [plan.llama_pair_for(c) for c in ("13.4", "12.8", "12.4", "12.2", "", None)] == [
        "cuda13", "cuda12", "cuda12", None, None, None]
