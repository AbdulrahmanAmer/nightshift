"""C8 Clipper: URL sources through the pinned yt-dlp (subprocess is mocked)."""
from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ns_common import ffmpeg
from ns_common.config import save_channel
from ns_common.errors import NsError, describe
from ns_common.paths import Paths
from ns_studio.clipper import cli
from ns_studio.clipper import source as src
from ns_studio.clipper import stages_clip as sc
from ns_studio.clipper.models import ClipParams
from ns_studio.context import FAKEABLE
from ns_studio.stages import run_stage
from tests.fixtures.clipper.make_sample import shared_sample
from tests.helpers import make_channel

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not available")
URL = "https://example.com/watch?v=abc123"
PROGRESS = [
    "[youtube] Extracting URL: https://example.com/watch?v=abc123",
    "[download] Destination: source.f137.mp4",
    "[download]   0.0% of 10.00MiB at  1.00MiB/s ETA 00:10",
    "[download]  12.5% of 10.00MiB at  2.00MiB/s ETA 00:04",
    "",
    "[download] 100% of 10.00MiB in 00:05",
    "[Merger] Merging formats into \"source.mp4\"",
]


def fake_popen(
    lines: list[str] | None = None,
    code: int = 0,
    write: bool = True,
    title: str | None = "My Clip Video",
    payload: Path | None = None,
):
    """A stand-in for `subprocess.Popen` that 'downloads' by writing files where `-o` points."""

    class FakePopen:
        calls: list[list[str]] = []
        kwargs: dict[str, Any] = {}

        def __init__(self, cmd: list[str], **kw: Any) -> None:
            FakePopen.calls.append(cmd)
            FakePopen.kwargs = kw
            target = Path(cmd[cmd.index("-o") + 1].replace("%(ext)s", "mp4"))
            if write:
                target.parent.mkdir(parents=True, exist_ok=True)
                if payload is not None:
                    shutil.copy2(payload, target)
                else:
                    target.write_bytes(b"video")
                if title is not None:
                    (target.parent / "source.info.json").write_text(
                        json.dumps({"title": title, "id": "abc123"}), encoding="utf-8"
                    )
            self.stdout = iter([f"{ln}\n" for ln in (PROGRESS if lines is None else lines)])

        def wait(self) -> int:
            return code

    return FakePopen


def fake_exe(home: Path) -> Path:
    exe = Paths(home=home).home / "bin" / "yt-dlp" / "yt-dlp.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    return exe


# ---------- helpers ----------
def test_is_url() -> None:
    assert src.is_url("https://example.com/v") and src.is_url("HTTP://example.com/v")
    assert not src.is_url("ftp://example.com/v") and not src.is_url("C:\\videos\\a.mp4")
    assert not src.is_url("https://exa mple.com") and not src.is_url("movie.mp4")


def test_ytdlp_path_is_under_bin(tmp_home: Path) -> None:
    assert src.ytdlp_path(Paths.from_env()) == tmp_home / "bin" / "yt-dlp" / "yt-dlp.exe"


def test_args_follow_the_pinned_recipe(tmp_path: Path) -> None:
    args = src.ytdlp_args(Path("yt-dlp.exe"), URL, tmp_path)
    assert args[0] == "yt-dlp.exe"
    assert args[args.index("-f") + 1] == "bv*[height<=1080]+ba/b"
    assert args[args.index("--merge-output-format") + 1] == "mp4"
    assert args[args.index("-o") + 1] == str(tmp_path / "source.%(ext)s")
    assert "--no-playlist" in args and "--newline" in args and "--write-info-json" in args
    assert args[-2:] == ["--", URL]


def test_download_code_is_registered_as_an_error() -> None:
    assert src.DOWNLOAD_FAILED == "NS-E409"
    assert describe("NS-E409").kind == "error"


# ---------- import_url ----------
def test_import_url_reports_progress_and_returns_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = fake_exe(tmp_path / "home")
    popen = fake_popen()
    monkeypatch.setattr(src.subprocess, "Popen", popen)
    seen: list[float] = []
    out = src.import_url(URL, tmp_path / "dl", seen.append, exe=exe)
    assert out == tmp_path / "dl" / "source.mp4" and out.read_bytes() == b"video"
    assert seen == [0.0, 12.5, 100.0]
    assert popen.calls[0][0] == str(exe) and popen.calls[0][-1] == URL
    assert popen.kwargs["text"] is True and popen.kwargs["stdout"] == src.subprocess.PIPE
    assert src.read_download_title(tmp_path / "dl") == "My Clip Video"


def test_progress_callback_is_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = fake_exe(tmp_path / "home")
    monkeypatch.setattr(src.subprocess, "Popen", fake_popen())
    assert src.import_url(URL, tmp_path / "dl", exe=exe).name == "source.mp4"


def test_nonzero_exit_is_e409_with_the_log_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = fake_exe(tmp_path / "home")
    lines = ["[youtube] abc: Downloading webpage", "ERROR: Video unavailable"]
    monkeypatch.setattr(src.subprocess, "Popen", fake_popen(lines, code=1, write=False))
    with pytest.raises(NsError) as ei:
        src.import_url(URL, tmp_path / "dl", exe=exe)
    assert ei.value.code == "NS-E409"
    assert "exit 1" in ei.value.detail and "Video unavailable" in ei.value.detail


def test_missing_binary_is_e409(tmp_path: Path) -> None:
    with pytest.raises(NsError) as ei:
        src.import_url(URL, tmp_path / "dl", exe=tmp_path / "nope" / "yt-dlp.exe")
    assert ei.value.code == "NS-E409" and "not found" in ei.value.detail


def test_non_http_link_is_e409(tmp_path: Path) -> None:
    with pytest.raises(NsError) as ei:
        src.import_url("ftp://example.com/v", tmp_path / "dl", exe=fake_exe(tmp_path / "home"))
    assert ei.value.code == "NS-E409"


def test_no_file_written_is_e409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = fake_exe(tmp_path / "home")
    monkeypatch.setattr(src.subprocess, "Popen", fake_popen(write=False))
    with pytest.raises(NsError) as ei:
        src.import_url(URL, tmp_path / "dl", exe=exe)
    assert ei.value.code == "NS-E409" and "no video file" in ei.value.detail


def test_cannot_start_the_process_is_e409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = fake_exe(tmp_path / "home")

    def boom(*a: Any, **k: Any) -> None:
        raise OSError("blocked")

    monkeypatch.setattr(src.subprocess, "Popen", boom)
    with pytest.raises(NsError) as ei:
        src.import_url(URL, tmp_path / "dl", exe=exe)
    assert ei.value.code == "NS-E409" and "blocked" in ei.value.detail


def test_find_download_skips_side_files(tmp_path: Path) -> None:
    (tmp_path / "source.webm").write_bytes(b"x" * 100)
    (tmp_path / "source.info.json").write_text("{}", encoding="utf-8")
    (tmp_path / "source.f137.mp4.part").write_bytes(b"x" * 500)
    assert src.find_download(tmp_path) == tmp_path / "source.webm"
    assert src.find_download(tmp_path / "empty") is None


def test_read_download_title_edge_cases(tmp_path: Path) -> None:
    assert src.read_download_title(tmp_path) is None
    (tmp_path / "source.info.json").write_text('{"title": "  "}', encoding="utf-8")
    assert src.read_download_title(tmp_path) is None
    (tmp_path / "source.info.json").write_text("not json", encoding="utf-8")
    assert src.read_download_title(tmp_path) is None


# ---------- the import stage ----------
def url_job(tmp_path: Path) -> Any:
    params = ClipParams(source=URL, channel="after-dark", rights_confirmed=True)
    return sc.create_clip_job(Paths.from_env(), make_channel(), params, fake=FAKEABLE)


@needs_ffmpeg
def test_import_stage_downloads_then_imports(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_exe(tmp_home)
    popen = fake_popen(payload=shared_sample().video, title="Raid Night: Part 1")
    monkeypatch.setattr(src.subprocess, "Popen", popen)
    ctx = url_job(tmp_path)
    assert run_stage(ctx, sc.ImportStage()) is True
    info = ctx.read_json("source.json")
    assert info["title"] == "Raid Night: Part 1" and info["original_name"] == "Raid Night: Part 1.mp4"
    assert info["origin"] == URL and Path(info["path"]).exists()
    assert (info["width"], info["height"]) == (1280, 720)
    assert not (tmp_home / "imports" / "clipper" / "_downloads").exists() or not list(
        (tmp_home / "imports" / "clipper" / "_downloads").iterdir()
    )
    row = ctx.db.one("SELECT title, rights_confirmed FROM sources WHERE source_id=?", (info["source_id"],))
    assert row is not None and row["title"] == "Raid Night: Part 1" and row["rights_confirmed"] == 1
    # a re-run with other options keeps the import and does not download again
    replace(ClipParams.load(ctx.job_dir), max_clips=2).save(ctx.job_dir)
    assert run_stage(ctx, sc.ImportStage()) is True
    assert len(popen.calls) == 1


def test_url_import_still_needs_the_rights_confirmation(tmp_home: Path, tmp_path: Path) -> None:
    params = ClipParams(source=URL, channel="after-dark", rights_confirmed=False)
    ctx = sc.create_clip_job(Paths.from_env(), make_channel(), params, fake=FAKEABLE)
    with pytest.raises(NsError) as ei:
        run_stage(ctx, sc.ImportStage())
    assert ei.value.code == "NS-E405"


def test_import_stage_reports_a_failed_download(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_exe(tmp_home)
    monkeypatch.setattr(src.subprocess, "Popen", fake_popen(["ERROR: 404"], code=1, write=False))
    ctx = url_job(tmp_path)
    with pytest.raises(NsError) as ei:
        run_stage(ctx, sc.ImportStage())
    assert ei.value.code == "NS-E409"


# ---------- CLI ----------
def test_cli_detects_a_url_and_reports_a_missing_downloader(tmp_home: Path) -> None:
    paths = Paths.from_env()
    paths.ensure()
    save_channel(paths, make_channel())
    args = ["https://example.com/v", "--channel", "after-dark", "--confirm-rights", "--fake-services"]
    res = CliRunner().invoke(cli.app, cli.normalize_fake_flag(args))
    assert res.exit_code == 1
    assert "NS-E409" in res.output and "yt-dlp" in res.output


def test_cli_still_asks_for_rights_with_a_url(tmp_home: Path) -> None:
    res = CliRunner().invoke(cli.app, ["https://example.com/v", "--channel", "after-dark"], input="n\n")
    assert res.exit_code == 1 and "Rights not confirmed" in res.output
