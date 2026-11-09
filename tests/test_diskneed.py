"""H-6 and D-33: one way of saying how much disk space something needs and how much is free.

The install half is computed from the manifest and from what is already on the drive; the job half comes from
the sizes the pipeline really writes. Neither reads a fixed figure.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from ns_common import diskneed
from ns_common.diskneed import GIB, MIB, install_need
from ns_common.errors import NsError, describe
from ns_common.paths import Paths

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads((ROOT / "manifest.json").read_text(encoding="utf-8-sig"))


def need_for(manifest: dict[str, Any], home: Path, **kw: Any) -> diskneed.Need:
    kw.setdefault("llama_pair", "cuda13")
    return install_need(manifest, home, **kw)


def put_verified(dest: Path, sha: str | None = None) -> None:
    """A file in place with the `.ok` mark the downloader writes (size + mtime + hash)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x" * 16)
    st = dest.stat()
    marker = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha}
    dest.with_name(dest.name + ".ok").write_text(json.dumps(marker), encoding="utf-8")


def place_models(manifest: dict[str, Any], home: Path, profile: str = "gpu", tier: str = "small") -> None:
    """Every model file of the plan, verified, as a finished first download leaves them."""
    paths = Paths(home)
    pm = manifest["profiles"][profile]
    llm = {
        "small": pm["llm"],
        "balanced": pm.get("llm_balanced") or pm["llm"],
        "best": pm.get("llm_best") or pm["llm"],
    }[tier]
    put_verified(paths.models_llm / Path(llm["file"]).name, llm.get("sha256"))
    put_verified(paths.models_embed / Path(pm["embed"]["file"]).name, pm["embed"].get("sha256"))
    for f in pm["tts"]:
        put_verified(paths.models_tts / Path(f["file"]).name, f.get("sha256"))
    wdir = paths.models_whisper / pm["whisper_model"]
    for name, sha in pm["whisper_repo"]["files_sha256"].items():
        put_verified(wdir / name, sha)
    yunet = manifest["bins"]["yunet"]
    put_verified(paths.home / "models" / "cv" / "yunet.onnx", yunet["sha256"])


def place_programs_and_venvs(home: Path) -> None:
    """What a finished install has besides the models: unpacked programs and two working environments."""
    paths = Paths(home)
    for sub, flags in (
        ("llama", ("llama_cuda13", "cudart13")),
        ("opencode", ("opencode",)),
        ("qwentts", ("qwentts",)),
        ("webview2", ("webview2",)),
    ):
        folder = paths.home / "bin" / sub
        folder.mkdir(parents=True, exist_ok=True)
        for flag in flags:
            (folder / f".ns-{flag}.done").write_text("done", encoding="utf-8")
    (paths.bin_llama / "llama-server.exe").write_bytes(b"x")
    (paths.bin_opencode / "opencode.exe").write_bytes(b"x")
    for exe in ("qwen-tts.exe", "qwen-codec.exe"):
        (paths.bin_qwentts / exe).write_bytes(b"x")
    pinned = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8-sig"))["bins"]["yt_dlp"]["sha256"]
    put_verified(paths.home / "bin" / "yt-dlp" / "yt-dlp.exe", pinned)
    (paths.bin_uv).mkdir(parents=True, exist_ok=True)
    (paths.bin_uv / "uv.exe").write_bytes(b"x")
    for name in ("studio", "supervisor"):
        site = paths.venvs / name / "Lib" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        for i in range(30):
            (site / f"pkg{i}").mkdir()
        (paths.venvs / name / "Scripts").mkdir(parents=True, exist_ok=True)
        (paths.venvs / name / "Scripts" / "python.exe").write_bytes(b"x")
    (paths.config).mkdir(parents=True, exist_ok=True)
    (paths.config / "answers.json").write_text("{}", encoding="utf-8")


# ----------------------------------------------------------------------------------------------- H-6: install
def test_a_full_small_install_on_an_empty_home_comes_out_at_the_specs_60_gb(manifest, tmp_path):
    need = need_for(manifest, tmp_path / "home")
    assert 59 <= need.gb <= 61, need.gb
    # it is a sum: the three groups add up to the headline
    assert sum(p.bytes for p in need.parts) == need.total
    keys = {p.key for p in need.parts}
    assert keys == {"download", "programs", "reserve"}


def test_the_bigger_tiers_add_exactly_the_bigger_model_files(manifest, tmp_path):
    small = need_for(manifest, tmp_path / "a", tier="small")
    pm = manifest["profiles"]["gpu"]
    for tier, key in (("balanced", "llm_balanced"), ("best", "llm_best")):
        bigger = need_for(manifest, tmp_path / "a", tier=tier)
        extra = pm[key]["bytes"] - pm["llm"]["bytes"]
        assert bigger.total - small.total == extra
        assert bigger.gb > small.gb


def test_the_working_reserve_is_stated_and_is_twelve_weeks_of_videos(manifest, tmp_path):
    need = need_for(manifest, tmp_path / "home")
    reserve = next(p for p in need.parts if p.key == "reserve")
    assert reserve.bytes == diskneed.RESERVE_BYTES
    assert "reserve" in reserve.label and "videos" in reserve.label
    # the reserve is derived from the job estimates, so the two cannot drift apart
    week = (
        diskneed.format_need("master_first", long_minutes=(12, 12), shorts_per_master=(5, 5)).total
        + 3 * diskneed.SHORT_BYTES
    )
    assert diskneed.RESERVE_BYTES >= 12 * week + diskneed.HEADROOM_BYTES


def test_a_rerun_over_downloaded_models_asks_for_far_less(manifest, tmp_path):
    home = tmp_path / "home"
    empty = need_for(manifest, home)
    place_models(manifest, home)
    again = need_for(manifest, home)
    models = next(p for p in empty.parts if p.key == "download")
    assert models.bytes > 13 * 1e9
    assert not any(p.key == "download" for p in again.parts)
    assert again.total == empty.total - models.bytes
    assert again.gb < empty.gb - 13  # 49 GB free is enough for it, 60 never was


def test_a_finished_install_is_not_asked_for_the_reserve_again(manifest, tmp_path):
    home = tmp_path / "home"
    place_models(manifest, home)
    place_programs_and_venvs(home)
    need = need_for(manifest, home)
    assert [p.key for p in need.parts] == ["headroom"]
    assert need.gb <= 2
    assert "no models to download" in need.makeup()


def test_a_half_downloaded_file_counts_only_what_is_left(manifest, tmp_path, monkeypatch):
    home = tmp_path / "home"
    paths = Paths(home)
    llm = manifest["profiles"]["gpu"]["llm"]
    before = need_for(manifest, home)
    paths.models_llm.mkdir(parents=True)
    part = paths.models_llm / (Path(llm["file"]).name + ".part")
    part.write_bytes(b"x" * 1000)  # a real file of 1 KB: sizes are faked, not written
    assert need_for(manifest, home).total == before.total - 1000
    # a bigger leftover is a faked size (no multi-GB file is ever written by a test)
    big_one = paths.models_llm / Path(llm["file"]).name
    monkeypatch.setattr(diskneed, "_part_bytes", lambda dest: 4_000_000_000 if dest == big_one else 0)
    after = need_for(manifest, home)
    assert before.total - after.total == 4_000_000_000
    # more bytes than the file has leaves nothing to fetch for it, never a negative need
    monkeypatch.setattr(diskneed, "_part_bytes", lambda dest: 10**12)
    assert need_for(manifest, home).total > 0


def test_a_stale_mark_counts_for_nothing(manifest, tmp_path):
    home = tmp_path / "home"
    paths = Paths(home)
    llm = manifest["profiles"]["gpu"]["llm"]
    full = need_for(manifest, home)
    dest = paths.models_llm / Path(llm["file"]).name
    put_verified(dest, llm["sha256"])
    assert need_for(manifest, home).total == full.total - llm["bytes"]
    dest.write_bytes(b"changed afterwards")  # size and mtime no longer match the mark
    assert need_for(manifest, home).total == full.total
    put_verified(dest, "0" * 64)  # marked for another hash
    assert need_for(manifest, home).total == full.total


def test_a_custom_one_gb_model_replaces_the_nine_gb_one(manifest, tmp_path):
    small = need_for(manifest, tmp_path / "a")
    custom = {"repo": "someone/tiny-gguf", "file": "tiny-1b-q4.gguf", "size": 1_000_000_000}
    need = need_for(manifest, tmp_path / "a", custom=custom)
    assert small.total - need.total == manifest["profiles"]["gpu"]["llm"]["bytes"] - 1_000_000_000
    assert need.gb <= small.gb - 7
    # a downloaded custom model is not asked for again
    put_verified(Paths(tmp_path / "a").models_llm / "tiny-1b-q4.gguf")
    assert need_for(manifest, tmp_path / "a", custom=custom).total == need.total - 1_000_000_000


def test_models_source_with_everything_is_a_copy_not_a_download(manifest, tmp_path):
    """The files are copied into the home (copy_from_source), so they still take their bytes there."""
    src = tmp_path / "usb"
    pm = manifest["profiles"]["gpu"]
    names = [
        Path(pm["llm"]["file"]).name,
        Path(pm["embed"]["file"]).name,
        *[Path(f["file"]).name for f in pm["tts"]],
        "model.bin",
        "yunet.onnx",
    ]
    for name in names:
        (src / "models").mkdir(parents=True, exist_ok=True)
        (src / "models" / name).write_bytes(b"x")
    online = need_for(manifest, tmp_path / "home")
    offline = need_for(manifest, tmp_path / "home", models_source=src)
    assert offline.total == online.total
    keys = {p.key for p in offline.parts}
    assert "copy" in keys and "download" not in keys
    assert "from your folder" in offline.makeup() and "to download" not in offline.makeup()


def test_the_engine_test_shape_is_about_one_gb_of_files_plus_the_reserve(manifest, tmp_path):
    """Fake voice, tiny whisper, a 640 MB test model on the cpu-dev profile (QA's engine install)."""
    m = copy.deepcopy(manifest)
    llm = m["profiles"]["cpu-dev"]["llm"]
    llm.update(repo="Qwen/Qwen3-0.6B-GGUF", file="Qwen3-0.6B-Q8_0.gguf", bytes=639_000_000)
    need = install_need(m, tmp_path / "home", profile="cpu-dev", voice=False, llama_pair=None)
    download = next(p for p in need.parts if p.key == "download")
    assert 0.8e9 < download.bytes < 1.1e9  # llm + embed + whisper tiny + face finder
    assert not any("voice" in p.label for p in need.parts)
    # the same install with a voice engine and the 14 GB model is the 60 GB one
    assert need.gb < 46
    assert need.total - download.bytes - diskneed.RESERVE_BYTES < 2.5 * GIB  # programs and environments only


def test_a_dev_install_keeps_its_ten_gb_rule(manifest, tmp_path):
    need = install_need(manifest, tmp_path / "home", profile="cpu-dev", dev=True)
    assert need.gb == 10 == diskneed.DEV_INSTALL_BYTES // GIB


def test_the_makeup_is_one_line_with_the_number_and_the_free_space(manifest, tmp_path):
    need = need_for(manifest, tmp_path / "home")
    line = need.sentence(49 * GIB, "D:", subject="This install")
    assert "\n" not in line
    assert line.startswith("This install needs about 60 GB (")
    assert "models to download" in line and "reserve for videos" in line
    assert line.endswith("D: has 49 GB free")
    assert need.makeup().count("\n") == 0


def test_the_pieces_are_the_files_the_downloaders_planner_names(manifest):
    """Same files, paths and sizes as `plan_models` and `plan_bins` (uv and the voice engine: own steps)."""
    from installer.stage2.downloader import plan_bins, plan_models
    from ns_common.config import load_manifest

    typed = load_manifest(ROOT / "manifest.json")
    home = Path("D:/ns-diskneed-parity")  # only named, never created
    for profile, tier, pair in (
        ("gpu", "small", "cuda13"),
        ("gpu", "balanced", "cuda12"),
        ("cpu-dev", "small", None),
    ):
        items = plan_models(typed, profile, tier, home) + plan_bins(
            typed, profile, home, include_code=True, llama_pair=pair
        )
        pieces = [
            p
            for p in diskneed.install_pieces(
                manifest, home, profile=profile, tier=tier, include_code=True, llama_pair=pair
            )
            if p.dest is not None and p.name not in ("qwentts",)
        ]
        planned = {Path(i.dest): i for i in items if not i.optional}
        assert {p.dest for p in pieces} <= set(planned), {p.dest for p in pieces} - set(planned)
        # every program the downloader fetches into _downloads, and every sized model, is a piece
        mine = {p.dest for p in pieces}
        assert {d for d in planned if d.parent.name == "_downloads"} <= mine
        by_dest = {p.dest: p for p in pieces}
        for dest, item in planned.items():
            piece = by_dest.get(dest)  # whisper's small side files have no piece of their own
            if item.size:
                assert piece is not None and piece.size == item.size, dest
            if piece is not None:
                assert piece.sha256 == item.sha256, dest


# --------------------------------------------------------------------------------------------- free space
class Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def warn(self, event: str, msg: str = "", **data: Any) -> None:
        self.events.append((event, msg))


def test_unknown_free_space_does_not_block_and_is_logged(manifest, tmp_path, monkeypatch):
    def boom(_p: Any) -> Any:
        raise OSError("the volume does not answer")

    monkeypatch.setattr(shutil, "disk_usage", boom)
    need = diskneed.short_need()
    log = Log()
    verdict = diskneed.check(need, tmp_path, log=log)
    assert verdict.ok and verdict.free is None
    assert [e for e, _m in log.events] == ["disk_free_unknown"]
    diskneed.require_room(need, tmp_path, log=log)  # does not raise either


def test_check_reads_the_volume_of_the_nearest_existing_folder(tmp_path, monkeypatch):
    seen: list[Path] = []

    class Usage:
        free = 7 * GIB

    def fake(p: Any) -> Any:
        seen.append(Path(p))
        return Usage()

    monkeypatch.setattr(shutil, "disk_usage", fake)
    verdict = diskneed.check(diskneed.short_need(), tmp_path / "not" / "made" / "yet")
    assert seen == [tmp_path] and verdict.ok and verdict.free == 7 * GIB


# ----------------------------------------------------------------------------------------------- D-33: jobs
def test_a_short_needs_about_100_mb_and_says_how_it_was_estimated():
    need = diskneed.short_need()
    assert need.total == diskneed.SHORT_BYTES == 100 * MIB
    assert {p.key for p in need.parts} == {"clips", "voice", "render", "scratch"}
    assert "stock clips" in need.makeup()


def test_a_full_video_is_far_more_and_grows_with_its_length():
    ten = diskneed.format_need("master_first", long_minutes=(10, 10), shorts_per_master=(0, 0))
    fourteen = diskneed.format_need("master_first", long_minutes=(14, 14), shorts_per_master=(0, 0))
    assert fourteen.total > ten.total > 1 * GIB
    assert fourteen.total > 10 * diskneed.SHORT_BYTES
    with_shorts = diskneed.format_need("master_first", long_minutes=(10, 10), shorts_per_master=(6, 6))
    assert with_shorts.total - ten.total == 6 * diskneed.SHORT_BYTES


def test_each_format_has_an_estimate_and_the_clip_job_depends_on_its_source():
    assert diskneed.format_need("shorts_only", count=3).total == 3 * diskneed.SHORT_BYTES
    assert diskneed.format_need("series_compilation").total > 1 * GIB
    local = diskneed.clip_need(max_clips=6)
    link = diskneed.clip_need(max_clips=6, url_source=True)
    assert link.total - local.total >= 1 * GIB
    assert diskneed.clip_need(max_clips=6, rerun=True).total < local.total
    with pytest.raises(ValueError):
        diskneed.format_need("no_such_format")


def test_fake_footage_makes_a_job_lighter_but_never_free():
    real = diskneed.short_need()
    fake = diskneed.short_need(real_footage=False)
    assert 0 < fake.total < real.total


def test_a_resumed_job_needs_only_what_its_unfinished_stages_write():
    full = diskneed.format_need("master_first", long_minutes=(12, 12), shorts_per_master=(5, 5))
    at_voice = full.remaining({"ideas", "long_plan", "chapters_draft"})
    assert at_voice.total == full.total  # nothing of the sized stages is done yet
    after_clips = full.remaining({"voice", "clips"})
    assert after_clips.total < full.total
    assert not any(p.key in ("voice", "clips") for p in after_clips.parts)
    finished = full.remaining({"voice", "clips", "render_master", "shorts_derive", "outbox"})
    assert 0 < finished.total <= diskneed.MIN_RESUME_BYTES  # a floor: the last file must still be written


def test_a_job_refused_for_room_says_what_it_needs_and_what_the_drive_has(tmp_path):
    need = diskneed.short_need()
    with pytest.raises(NsError) as ei:
        diskneed.require_room(need, tmp_path, free=20 * MIB)
    e = ei.value
    assert e.code == "NS-E104"
    drive = tmp_path.drive or tmp_path.anchor
    assert e.detail.startswith("This needs about 100 MB (") and e.detail.endswith(f"{drive} has 20 MB free")
    assert e.data["need_bytes"] == need.total and e.data["free_bytes"] == 20 * MIB
    assert diskneed.require_room(need, tmp_path, free=need.total).ok  # exactly enough is enough


# ---------------------------------------------------------------------------------------- the error texts
def test_ns_e104_no_longer_quotes_the_installs_60_gb_rule_for_a_job():
    d = describe("NS-E104")
    assert "60 GB" not in d.hint and "80 GB" not in d.hint
    assert "message" in d.hint and "NIGHTSHIFT_HOME" in d.hint


def test_ns_w303_covers_the_start_up_sentence_case_too():
    hint = describe("NS-W303").hint
    assert "missing or failed its start-up test" in hint
    assert "This PC has no Windows speech voice" not in hint
