"""C1: stage 1's hand-over JSON becomes plain check rows, fix buttons and meters for the window."""

import json
import zipfile
from pathlib import Path

import pytest

from installer.stage2.web import fakes, plan, preflight

ROOT = Path(__file__).resolve().parents[1]


def handover(**changes) -> dict:
    """The sample hand-over with some rows replaced: ``changes`` maps a row id to (status, code, value)."""
    pf = fakes.sample_handover(ROOT, home="D:\\Nightshift")
    for row in pf["checks"]:
        if row["id"] in changes:
            row["status"], row["code"], row["value"] = changes[row["id"]]
    return pf


def row(pf: dict, row_id: str, home: str | None = None) -> dict:
    return next(r for r in preflight.check_rows(pf, home) if r["id"] == row_id)


def test_a_good_pc_has_eleven_ok_rows_and_nothing_blocks():
    rows = preflight.check_rows(handover(), "D:\\Nightshift")
    assert [r["id"] for r in rows] == list(preflight.CHECK_IDS)
    assert all(r["status"] == "ok" and not r["blocking"] for r in rows)
    assert all(r["title"] is None and r["fix"] is None for r in rows)
    assert row(handover(), "gpu")["label"] == "Graphics card"


@pytest.mark.parametrize(
    ("row_id", "change", "title", "fix_kind", "blocking"),
    [
        ("gpu", ("fail", "NS-E102", "nvidia-smi found no GPU"), "No NVIDIA graphics card found",
         "open-url", True),
        ("driver", ("fail", "NS-E103", "CUDA 12.2"), "NVIDIA driver too old", "open-url", True),
        ("driver", ("warn", "NS-E103", "CUDA 12.4"), "NVIDIA driver too old", "open-url", False),
        ("disk", ("fail", "NS-E104", "31 GB free on D:"), "Not enough free disk space", "choose-drive", True),
        ("disktype", ("warn", "NS-W106", "D: is a hard disk"), "The install drive is a hard disk",
         "choose-drive", False),
        ("winget", ("fail", "NS-E111", "winget --version failed"), "winget is not available",
         "open-url", True),
        ("longpaths", ("warn", "NS-W112", "LongPathsEnabled is not 1"), "Long file paths are off",
         "elevate-longpaths", False),
        ("waketimers", ("warn", "NS-W107", "disabled in the power plan"),
         "The PC cannot wake itself at night", None, False),
        ("os", ("fail", "NS-E101", "architecture ARM64"), "This PC cannot run Nightshift", None, True),
    ],
)
def test_rows_get_plain_titles_fix_buttons_and_blocking(row_id, change, title, fix_kind, blocking):
    pf = handover(**{row_id: change})
    r = row(pf, row_id)
    assert r["title"] == title and r["whatToDo"] and r["code"] == change[1]
    assert r["blocking"] is blocking
    assert (r["fix"] or {}).get("kind") == fix_kind
    if fix_kind == "open-url":
        assert r["fix"]["url"].startswith(("https://www.nvidia.com", "ms-windows-store://"))
    assert not r["whatToDo"].endswith("!") and "NS-" not in r["whatToDo"]       # a code never stands alone


def test_rows_without_a_code_still_say_what_to_do():
    low_vram = row(handover(gpu=("warn", "", "RTX, only 8192 MB of video memory")), "gpu")
    assert low_vram["title"] == "The graphics card has less than 16 GB" and low_vram["code"] is None
    low_ram = row(handover(ram=("warn", "", "8 GB is below the 16 GB minimum")), "ram")
    assert low_ram["title"] == "Less memory than recommended" and "Small model" in low_ram["whatToDo"]
    low_disk = row(handover(disk=("warn", "", "50 GB free on D: (60 GB recommended)")), "disk")
    assert low_disk["title"] == "Less free space than recommended"
    assert low_disk["fix"]["kind"] == "choose-drive" and not low_disk["blocking"]


def test_only_hugging_face_offline_is_a_warning_and_everything_offline_blocks():
    only_hf = handover(network=("warn", "NS-E105", "unreachable: huggingface.co"))
    r = row(only_hf, "network")
    assert not r["blocking"] and "Hugging Face" in r["whatToDo"] and r["fix"]["kind"] == "recheck"
    only_hf["unreachable"] = ["huggingface.co"]
    assert preflight.hf_only_blocked(only_hf)
    offline = handover(network=("fail", "NS-E105", "unreachable: github.com, pypi.org"))
    r = row(offline, "network")
    assert r["blocking"] and "Check the internet" in r["whatToDo"]
    offline["unreachable"] = ["github.com", "pypi.org"]
    assert not preflight.hf_only_blocked(offline)
    assert not preflight.hf_only_blocked(handover())


def test_disk_rows_are_worked_out_again_for_another_install_folder():
    pf = handover()
    pf["drives"] = [
        {"letter": "C", "free_gb": 31.0, "type": "SSD"}, {"letter": "D", "free_gb": 412.0, "type": "HDD"},
        {"letter": "E", "free_gb": 50.0, "type": "SSD"},
    ]
    c_disk = row(pf, "disk", "C:\\Nightshift")
    assert c_disk["status"] == "fail" and c_disk["blocking"]
    assert row(pf, "disk", "E:\\Nightshift")["status"] == "warn"
    assert row(pf, "disk", "D:\\Nightshift")["status"] == "ok"
    assert row(pf, "disktype", "D:\\Nightshift")["status"] == "warn"
    assert row(pf, "disktype", "E:\\Nightshift")["status"] == "ok"
    gone = row(pf, "disk", "Z:\\Nightshift")
    assert gone["status"] == "fail" and gone["code"] == "NS-E104"


def test_machine_summary_and_meters():
    pf = handover()
    m = preflight.machine_summary(pf, "D:\\Nightshift", "small")
    assert m["windows"] == "Windows 11 Pro 10.0.26200" and m["gpuName"] == "NVIDIA GeForce RTX 5060 Ti"
    assert m["vramMb"] == 16311 and m["cuda"] == "13.0" and m["ramGb"] == 32.0 and m["profile"] == "gpu"
    assert m["meters"]["vram"]["haveGb"] == 15.9 and m["meters"]["vram"]["needGb"] == 16
    assert m["meters"]["disk"] == {"haveGb": 500.0, "needGb": 60, "hardMinGb": 40, "path": "D:\\Nightshift"}
    assert preflight.machine_summary(pf, "D:\\Nightshift", "best")["meters"]["disk"]["needGb"] == 81
    assert {d["letter"] for d in m["drives"]} >= {"C", "D"}
    no_gpu = dict(pf, gpu={"name": "", "vram_mb": 0, "driver": ""}, cuda="")
    s = preflight.machine_summary(no_gpu, "D:\\Nightshift", "small")
    assert s["gpuName"] is None and s["vramMb"] is None and s["cuda"] is None


def test_disk_check_messages():
    drives = [{"letter": "D", "freeGb": 412.0, "type": "SSD"}, {"letter": "C", "freeGb": 31.0, "type": "HDD"}]
    ok = preflight.disk_check("D:\\Nightshift", "small", drives, lambda _p: 0.0)
    assert ok["ok"] and ok["message"] == "412 GB free on D: (SSD)"
    assert ok["drive"] == "D" and ok["needGb"] == 60
    low = preflight.disk_check("C:\\Nightshift", "small", drives, lambda _p: 0.0)
    assert not low["ok"] and low["message"] == "Only 31 GB free on C:. Nightshift needs 60 GB"
    asked = preflight.disk_check("F:\\x", "small", drives, lambda _p: 77.0)
    assert asked["ok"] and asked["freeGb"] == 77.0
    bad = preflight.disk_check("not a path", "small", drives, lambda _p: 0.0)
    assert not bad["ok"] and "full path" in bad["message"]

    def boom(_p: str) -> float:
        raise OSError("no such drive")

    assert "was not found" in preflight.disk_check("Q:\\x", "small", drives, boom)["message"]


def test_models_folder_scan_counts_loose_files_and_files_inside_zips(tmp_path: Path):
    manifest = plan.load_manifest(ROOT)
    expected = preflight.expected_model_files(manifest, "gpu", "small")
    assert len(expected) == len(set(expected)) and "Qwen3-14B-Q4_K_M.gguf" in expected
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / expected[0]).write_bytes(b"x" * 10)
    with zipfile.ZipFile(tmp_path / "part1.zip", "w") as zf:
        zf.writestr(f"models/{expected[1]}", b"y" * 20)
        zf.writestr("models/other.bin", b"z")
    (tmp_path / "broken.zip").write_bytes(b"not a zip")
    found = preflight.scan_models_source(tmp_path, expected)
    assert found == {"files": 2, "of": len(expected), "bytes": 30}
    check = preflight.disk_check(str(tmp_path), "small", [], lambda _p: 1.0, models=found)
    assert check["ok"] and check["message"] == f"Found 2 of {len(expected)} model files, 0.0 GB"
    empty = preflight.scan_models_source(tmp_path / "missing", expected)
    assert empty["files"] == 0
    assert preflight.disk_check("E:\\none", "small", [], lambda _p: 1.0, models=empty)["message"] == (
        "That folder was not found")


def test_reprobe_runs_stage1_probe_only_and_reads_its_file(tmp_path: Path):
    out = tmp_path / "po.json"
    calls: list[list[str]] = []

    def good(argv):
        calls.append(list(argv))
        out.write_text(json.dumps(handover()), encoding="utf-8")
        return 0

    pf = preflight.reprobe(ROOT, out, "D:\\Nightshift", run=good)
    assert pf is not None and pf["schema"] == 1
    argv = calls[0]
    assert "-ProbeOnly" in argv and "-Out" in argv and "-HomePath" in argv and argv[0] == "powershell.exe"
    assert preflight.reprobe(ROOT, out, None, run=lambda _a: 1) is None
    assert preflight.reprobe(ROOT, out, None, run=lambda _a: 0) is None          # exit 0 but no file written


def test_copy_falls_back_to_the_errors_json_text():
    assert preflight.copy_for("NS-E109")[0] == "A downloaded file is damaged"
    assert preflight.copy_for("NS-X999", "It broke", "Try again") == ("It broke", "Try again")
    title, what = preflight.copy_for("unexpected")
    assert title == "Something unexpected stopped setup" and "Copy diagnostics" in what
