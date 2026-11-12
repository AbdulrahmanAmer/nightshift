"""WP8 (block G): one size formatter in Windows units, the one nvidia-smi reader, the kit's user-name mask
that only masks a name, the kit's plan that says the Dashboard may be watched, programs by full path."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from rich.console import Console
from rich.progress import Progress, TextColumn

from dev.kit import gpukit
from installer.stage2.downloader import human_bytes
from installer.stage2.ui import RichReporter, SizeColumn, SpeedColumn
from ns_common import nvsmi, sizes

GIB = 1024**3


def test_size_text_is_binary_and_labelled_gb_mb_kb() -> None:
    assert sizes.size_text(197_895_757_824) == "184.3 GB"  # G-31: what Windows shows, not 197.9
    assert sizes.size_text(7_877) == "7.7 KB"  # G-33: never "0.0 MB"
    assert sizes.size_text(0) == "0 B" and sizes.size_text(1023) == "1023 B"
    assert sizes.size_text(5 * 1024**2) == "5.0 MB"
    assert sizes.size_text(None) == "?"
    assert human_bytes(20_900_000_000) == sizes.size_text(20_900_000_000) == "19.5 GB"


def test_a_pair_and_a_speed_use_the_same_units() -> None:
    assert sizes.pair_text(int(8.4 * GIB), int(20.9 * GIB)) == "8.4/20.9 GB"
    assert sizes.pair_text(10 * 1024, 5 * 1024**2) == "0.0/5.0 MB"
    assert sizes.speed_text(12 * 1024**2) == "12.0 MB/s"
    assert sizes.speed_text(20 * 1024) == "20 KB/s"
    assert sizes.gb_value(197_895_757_824) == 184.3


def test_the_rich_bars_show_binary_units_not_rich_decimal() -> None:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=200, color_system=None)
    with Progress(TextColumn("{task.description}"), SizeColumn(), SpeedColumn(), console=console) as progress:
        task = progress.add_task("x", total=int(20.9 * GIB))
        progress.update(task, completed=int(8.4 * GIB))
    out = buf.getvalue()
    assert "8.4/20.9 GB" in out
    assert "9.0/22.4" not in out  # (what rich's decimal DownloadColumn says for the same bytes)


def test_the_plain_lines_use_the_same_formatter_for_a_tiny_file() -> None:
    buf = io.StringIO()
    with RichReporter(Console(file=buf, force_terminal=False), 7_877, files=1) as rep:
        rep.start("a.bin", 7_877)
    assert "a.bin: starting, 7.7 KB" in buf.getvalue()


BANNER_621 = """\
| NVIDIA-SMI 621.01                 KMD Version: 621.01      CUDA UMD Version: 13.2     |
"""
BANNER_OLD = "| NVIDIA-SMI 580.88   Driver Version: 580.88   CUDA Version: 13.0 |\n"


def test_the_driver_comes_from_the_query_column_and_the_621_banner_is_read_too() -> None:
    query = "NVIDIA GeForce RTX 5060 Ti, 621.01, 16311 MiB, 812 MiB, 15300 MiB\n"
    assert nvsmi.driver_from_query(query) == "621.01"
    assert nvsmi.driver_from_banner(BANNER_621) == "621.01"  # "KMD Version:" (G-1)
    assert nvsmi.cuda_from_banner(BANNER_621) == "13.2"  # "CUDA UMD Version:"
    assert nvsmi.driver_from_banner(BANNER_OLD) == "580.88" and nvsmi.cuda_from_banner(BANNER_OLD) == "13.0"
    assert nvsmi.read_driver(query, "no banner at all") == "621.01"  # the query alone is enough
    assert nvsmi.read_driver("", BANNER_621) == "621.01"  # the banner when the query gave none
    assert nvsmi.read_driver("name, [N/A], 1 MiB", None) is None  # "driver None" only when nothing says it


def test_the_kit_records_the_621_driver_and_shows_sizes_in_windows_units() -> None:
    assert gpukit._mib_text("16311 MiB") == "15.9 GB"
    assert gpukit._mib_text(None) == "None"


def test_a_user_name_that_is_a_common_word_is_masked_only_as_a_name() -> None:
    masks = gpukit.Masks(home=r"D:\Nightshift", user="readings", profile=r"C:\Users\readings",
                         computer="PC-7")
    kit_sentences = "the GPU readings' own readings, recorded this readings, stored on this readings"
    text = "\n".join([
        kit_sentences,
        r"profile C:\Users\readings\Desktop and PC-7\readings and logs/readings.jsonl",
        "USERNAME=readings",
        '{"owner": "readings", "user": "readings"}',
    ])
    out = gpukit.scrub_text(text, masks)
    assert out.splitlines()[0] == kit_sentences  # the kit's own words are not touched (G-35)
    assert "<profile>\\Desktop and <pc>\\<user> and logs/<user>.jsonl" in out
    assert "USERNAME=<user>" in out
    assert '{"owner": "<user>", "user": "<user>"}' in out
    assert not re.search(r"(Users|\\|/)readings", out)


def test_the_plan_says_the_dashboard_may_be_opened_and_watched() -> None:
    plan = " ".join(gpukit.PLAN)
    assert "open Nightshift's Dashboard (Queue page) and watch" in plan
    assert "start nothing there until the kit has ended" in plan


def test_every_program_the_kit_starts_has_a_full_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    ps = gpukit.powershell_exe().replace("/", "\\")
    assert ps == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    assert gpukit.windows_program("powercfg.exe").replace("/", "\\") == r"C:\Windows\System32\powercfg.exe"
    monkeypatch.delenv("SystemRoot")
    monkeypatch.delenv("windir", raising=False)
    assert gpukit.windows_program("powercfg.exe") == "powercfg.exe"  # not Windows: the bare name


def test_the_voice_engine_builds_its_argv_with_an_absolute_program_path(tmp_path: Path) -> None:
    from ns_tts.engine import QwenttsEngine

    eng = QwenttsEngine.__new__(QwenttsEngine)  # only `_argv` is under test: no models, no process
    eng.launcher = {}
    eng.bin_dir = Path("relative") / "bin"
    argv = eng._argv("qwen-tts")
    assert Path(argv[0]).is_absolute() and Path(argv[0]).name.startswith("qwen-tts")


def test_gpu_kit_cmd_names_powershell_by_its_full_path() -> None:
    cmd = (Path(__file__).resolve().parents[1] / "GPU-KIT.cmd").read_text(encoding="ascii")
    assert not re.search(r"(?mi)^powershell\b", cmd)
    assert r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" in cmd
