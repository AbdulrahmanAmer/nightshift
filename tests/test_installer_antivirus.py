"""C1: Windows Security removing or blocking a file is NS-E113 with the allow-list hint.

Defender flags unsigned PyInstaller and self-built programs (``yt-dlp.exe``, ``llama-server.exe``,
``qwen-tts.exe``) from time to time. Before, WinError 225 on a plain download became NS-E105 ("download site
cannot be reached") after three tries, a zip that failed to unpack escaped ``run_plan`` as a bare OSError
that the window called "unexpected", and a program removed after unpacking showed up as "the story model did
not pass" (NS-E203) at the end of a 20 GB download. Each case is faked here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from installer.stage2 import downloader as dl
from installer.stage2.steps import s08_smoke
from installer.stage2.steps.s02_download_bins import S02DownloadBins
from installer.stage2.testing import example_answers, make_test_context
from ns_common.avblock import is_blocked_error, looks_blocked
from ns_common.errors import NsError, describe
from tests.test_downloader import DATA, SHA, _zip_bytes, make_client, no_sleep

VIRUS = "Operation did not complete successfully because the file contains a virus or unwanted software"


def winerror_225() -> OSError:
    err = OSError(22, VIRUS)
    err.winerror = 225  # type: ignore[attr-defined]
    return err


def test_the_recogniser_knows_winerror_225_and_the_windows_wording() -> None:
    assert is_blocked_error(winerror_225())
    assert is_blocked_error(OSError(VIRUS))
    assert not is_blocked_error(OSError("connection reset by peer"))
    assert not is_blocked_error(ValueError("virus"))                  # only OS errors count
    assert looks_blocked("error: os error 225") and looks_blocked("Blocked by your organization")
    assert not looks_blocked("error: connection reset")


# ---- a plain file download ------------------------------------------------------------------------
def test_winerror_225_while_downloading_is_ns_e113_and_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def blocked(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        raise winerror_225()

    monkeypatch.setattr(dl, "_fetch_once", blocked)
    with pytest.raises(NsError) as ei:
        dl.download("https://x.test/yt-dlp.exe", tmp_path / "yt-dlp.exe", SHA, client=make_client(),
                    sleep=no_sleep)
    assert ei.value.code == "NS-E113" and "yt-dlp.exe" in ei.value.detail and calls == [1]
    assert "allow list" in ei.value.definition.hint


def test_any_other_os_error_is_still_a_download_problem_after_three_tries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def flaky(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        raise OSError("connection reset")

    monkeypatch.setattr(dl, "_fetch_once", flaky)
    with pytest.raises(NsError) as ei:
        dl.download("https://x.test/a.bin", tmp_path / "a.bin", SHA, client=make_client(), sleep=no_sleep)
    assert ei.value.code == "NS-E105" and len(calls) == 3


def test_a_download_that_vanishes_between_write_and_verify_is_ns_e113(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "bin" / "yt-dlp.exe"

    def scanner_takes_it(path: Path, digest: str | None) -> None:
        path.unlink()                                               # quarantined before the marker is written
        raise FileNotFoundError(str(path))

    monkeypatch.setattr(dl, "write_marker", scanner_takes_it)
    with pytest.raises(NsError) as ei:
        dl.download("https://x.test/yt-dlp.exe", dest, SHA, client=make_client(), sleep=no_sleep)
    assert ei.value.code == "NS-E113" and "disappeared" in ei.value.detail and not dest.exists()


def test_a_download_that_vanishes_after_the_marker_is_ns_e113_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "yt-dlp.exe"
    real = dl.write_marker

    def marker_then_scan(path: Path, digest: str | None) -> None:
        real(path, digest)
        path.unlink()

    monkeypatch.setattr(dl, "write_marker", marker_then_scan)
    with pytest.raises(NsError) as ei:
        dl.download("https://x.test/yt-dlp.exe", dest, SHA, client=make_client(DATA), sleep=no_sleep)
    assert ei.value.code == "NS-E113"


# ---- a zip ----------------------------------------------------------------------------------------
def zip_item(tmp_path: Path, body: bytes) -> dl.DownloadItem:
    return dl.DownloadItem(
        name="llama", url="https://x.test/l.zip", dest=tmp_path / "dl" / "l.zip",
        sha256=hashlib.sha256(body).hexdigest(), kind="zip", extract_to=tmp_path / "bin" / "llama",
        expect=("llama-server.exe",),
    )


def test_a_zip_that_cannot_be_unpacked_because_of_the_scanner_is_ns_e113_not_a_bare_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _zip_bytes({"llama-server.exe": b"MZ"})

    def blocked(zip_path: Path, target: Path) -> list[str]:
        raise winerror_225()

    monkeypatch.setattr(dl, "extract_zip", blocked)
    with pytest.raises(NsError) as ei:
        dl.run_plan([zip_item(tmp_path, body)], client=make_client(body))
    assert ei.value.code == "NS-E113"


def test_a_zip_member_removed_right_after_unpacking_is_ns_e113_but_a_zip_without_it_is_ns_e109(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _zip_bytes({"llama-server.exe": b"MZ", "ggml.dll": b"x"})

    def unpack_then_scan(zip_path: Path, target: Path) -> list[str]:
        target.mkdir(parents=True, exist_ok=True)
        (target / "ggml.dll").write_bytes(b"x")                    # llama-server.exe was in the zip, now gone
        return ["llama-server.exe", "ggml.dll"]

    with monkeypatch.context() as m:
        m.setattr(dl, "extract_zip", unpack_then_scan)
        with pytest.raises(NsError) as ei:
            dl.run_plan([zip_item(tmp_path, body)], client=make_client(body))
    assert ei.value.code == "NS-E113" and "llama-server.exe" in ei.value.detail
    assert "removed right after unpacking" in ei.value.detail
    wrong = _zip_bytes({"other.txt": b"x"})                       # the archive itself lacks the program
    with pytest.raises(NsError) as ei2:
        dl.run_plan([zip_item(tmp_path / "b", wrong)], client=make_client(wrong))
    assert ei2.value.code == "NS-E109"


# ---- later steps ----------------------------------------------------------------------------------
def test_s02_verify_names_a_program_that_was_unpacked_and_then_removed(tmp_path: Path) -> None:
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    S02DownloadBins().run(ctx)
    (ctx.paths.bin_llama / "llama-server.exe").unlink()
    with pytest.raises(NsError) as ei:
        S02DownloadBins().verify(ctx)
    assert ei.value.code == "NS-E113" and "llama-server.exe" in ei.value.detail


def smoke_ctx(tmp_path: Path) -> Any:
    return make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, simulate=False)


def test_the_smoke_test_reports_a_removed_llama_server_as_ns_e113_before_ns_e203(tmp_path: Path) -> None:
    ctx = smoke_ctx(tmp_path)
    ctx.paths.bin_llama.mkdir(parents=True, exist_ok=True)
    s08_smoke.check_programs_present(ctx)                        # nothing was unpacked: nothing to say
    (ctx.paths.bin_llama / ".ns-llama_cuda13.done").write_text("ok", encoding="utf-8")
    with pytest.raises(NsError) as ei:
        s08_smoke.check_programs_present(ctx)
    assert ei.value.code == "NS-E113" and "llama-server.exe" in ei.value.detail
    (ctx.paths.bin_llama / "llama-server.exe").write_bytes(b"MZ")
    s08_smoke.check_programs_present(ctx)                        # back: fine


def test_the_smoke_test_reports_removed_voice_programs_before_the_pytorch_fallback(tmp_path: Path) -> None:
    ctx = smoke_ctx(tmp_path)
    ctx.paths.bin_qwentts.mkdir(parents=True, exist_ok=True)
    (ctx.paths.bin_qwentts / ".ns-qwentts.done").write_text("ok", encoding="utf-8")
    (ctx.paths.bin_qwentts / "qwen-codec.exe").write_bytes(b"MZ")        # qwen-tts.exe is gone
    with pytest.raises(NsError) as ei:
        s08_smoke.check_programs_present(ctx)
    assert ei.value.code == "NS-E113" and "qwen-tts.exe" in ei.value.detail
    assert "qwen-codec.exe" not in ei.value.detail
    (ctx.paths.bin_qwentts / "qwen-tts.exe").write_bytes(b"MZ")
    s08_smoke.check_programs_present(ctx)


def test_a_simulation_does_not_judge_the_files_of_the_target_pc(tmp_path: Path) -> None:
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, simulate=True)
    ctx.paths.bin_llama.mkdir(parents=True, exist_ok=True)
    (ctx.paths.bin_llama / ".ns-llama_cuda13.done").write_text("ok", encoding="utf-8")
    s08_smoke.check_programs_present(ctx)


def test_the_fix_hint_of_ns_e113_is_the_allow_list_and_the_operator_copy_says_where_to_click() -> None:
    assert "allow list" in describe("NS-E113").hint
    root = Path(__file__).resolve().parents[1]
    copy = json.loads((root / "installer" / "stage2" / "web" / "operator_copy.json").read_text("utf-8"))
    assert "Protection history" in copy["NS-E113"]["whatToDo"]
    sac = "Windows Security > App & browser control > Smart App Control settings"
    assert sac in copy["NS-E113"]["whatToDo"] and "cannot allow a single file" in copy["NS-E113"]["whatToDo"]
    assert sac in describe("NS-E113").hint
    assert "Settings" not in copy["NS-E116"]["whatToDo"] and "restart the PC" in copy["NS-E116"]["whatToDo"]
    assert "change the port" not in describe("NS-E116").hint


def test_httpx_is_not_involved_in_the_blocked_case() -> None:
    assert not is_blocked_error(httpx.ConnectError("offline"))
