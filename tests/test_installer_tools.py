"""C1: b01 installs the Visual C++ runtime and FFmpeg (winget first, a pinned download second); no Git.

The llama.cpp and qwentts.cpp programs need ``MSVCP140.dll`` and ``VCRUNTIME140.dll``. A clean Windows 11 has
neither, and the symptom used to show only at the smoke test, after the whole download. Winget can also be
missing or refuse (a broken App Installer, a source that does not answer), so FFmpeg and the runtime each
have a pinned direct download in ``manifest.json``.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from installer.stage2.steps import s08_smoke
from installer.stage2.web import bootstrap
from installer.stage2.web.bootstrap import Boot, BootstrapError, CmdResult
from ns_common import ffmpeg, vcruntime
from ns_common.errors import NsError

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
PINS = {k: MANIFEST["bins"][k] for k in ("vcredist", "ffmpeg")}


# ---- the DLL check --------------------------------------------------------------------------------
def test_missing_lists_the_runtime_dlls_that_are_not_in_the_folder(tmp_path: Path) -> None:
    assert vcruntime.REQUIRED_DLLS == ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
    assert vcruntime.missing(tmp_path) == list(vcruntime.REQUIRED_DLLS)
    (tmp_path / "msvcp140.dll").write_bytes(b"MZ")
    (tmp_path / "vcruntime140.dll").write_bytes(b"MZ")
    assert vcruntime.missing(tmp_path) == ["vcruntime140_1.dll"]          # the second half counts too
    (tmp_path / "vcruntime140_1.dll").write_bytes(b"MZ")
    assert vcruntime.missing(tmp_path) == []


def test_the_default_folder_is_system32_under_systemroot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    assert vcruntime.system_dir() == tmp_path / "System32"


def test_stage1_probe_looks_for_the_same_three_files() -> None:
    text = (ROOT / "installer" / "stage1.ps1").read_text(encoding="utf-8")
    body = text[text.index("function Test-VcRuntime") : text.index("function ConvertTo-WakeSetting")]
    for name in vcruntime.REQUIRED_DLLS:
        assert f"'{name}'" in body, name
    assert "GetFolderPath('System')" in body


# ---- the pins -------------------------------------------------------------------------------------
def test_the_vcredist_pin_is_the_resolved_versioned_url_whose_path_carries_its_sha256() -> None:
    pin = MANIFEST["bins"]["vcredist"]
    assert re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])
    assert pin["url"].startswith("https://download.visualstudio.microsoft.com/download/pr/")
    assert pin["sha256"].upper() in pin["url"]            # Microsoft puts the file's hash in the final path
    assert pin["url"].endswith("/VC_redist.x64.exe") and "aka.ms" not in pin["url"]
    assert re.fullmatch(r"14\.\d+\.\d+\.\d+", pin["version"])


def test_the_ffmpeg_pin_is_a_fixed_btbn_release_not_the_moving_latest() -> None:
    pin = MANIFEST["bins"]["ffmpeg"]
    assert re.fullmatch(r"[0-9a-f]{64}", pin["sha256"])
    assert "/BtbN/FFmpeg-Builds/releases/download/autobuild-20" in pin["url"]
    assert "/download/latest/" not in pin["url"]
    assert pin["url"].endswith("-win64-gpl-8.1.zip") and "shared" not in pin["url"]    # static: two exes


# ---- download_file --------------------------------------------------------------------------------
class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def opener_for(data: bytes):
    def opener(url: str, timeout: float = 0) -> FakeResponse:
        return FakeResponse(data)

    return opener


def test_download_file_checks_the_digest_and_leaves_only_the_finished_file(tmp_path: Path) -> None:
    data = b"vc redist " * 1000
    dest = tmp_path / "dl" / "vc_redist.x64.exe"
    digest = hashlib.sha256(data).hexdigest()
    bootstrap.download_file("https://x/y.exe", digest, dest, opener=opener_for(data))
    assert dest.read_bytes() == data and not (dest.parent / "vc_redist.x64.exe.part").exists()


def test_download_file_with_a_wrong_digest_is_ns_e109_and_keeps_no_file(tmp_path: Path) -> None:
    dest = tmp_path / "f.exe"
    with pytest.raises(BootstrapError) as ei:
        bootstrap.download_file("https://x/y.exe", "0" * 64, dest, opener=opener_for(b"data"))
    assert ei.value.code == "NS-E109" and not dest.exists() and not (tmp_path / "f.exe.part").exists()


def test_download_file_retries_a_dropped_connection_then_gives_up_with_ns_e105(tmp_path: Path) -> None:
    calls: list[str] = []

    def opener(url: str, timeout: float = 0) -> FakeResponse:
        calls.append(url)
        raise OSError("connection reset")

    waits: list[float] = []
    with pytest.raises(BootstrapError) as ei:
        bootstrap.download_file(
            "https://x/y.exe", "0" * 64, tmp_path / "f.exe", opener=opener, sleep=waits.append
        )
    assert ei.value.code == "NS-E105" and len(calls) == 3 and len(waits) == 2


def test_download_file_maps_winerror_225_to_the_antivirus_code_without_retrying(tmp_path: Path) -> None:
    calls: list[str] = []

    def opener(url: str, timeout: float = 0) -> FakeResponse:
        calls.append(url)
        err = OSError(22, "Operation did not complete successfully because the file contains a virus")
        err.winerror = 225  # type: ignore[attr-defined]
        raise err

    with pytest.raises(BootstrapError) as ei:
        bootstrap.download_file("https://x/y.exe", "0" * 64, tmp_path / "f.exe", opener=opener)
    assert ei.value.code == "NS-E113" and len(calls) == 1


def test_a_download_that_vanishes_after_the_rename_is_ns_e113(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = bootstrap.os.replace

    def replace_then_scan(src: Any, dst: Any) -> None:       # the scanner deletes it as soon as it appears
        real_replace(src, dst)
        Path(dst).unlink()

    monkeypatch.setattr(bootstrap.os, "replace", replace_then_scan)
    data = b"payload"
    with pytest.raises(BootstrapError) as ei:
        bootstrap.download_file(
            "https://x/y.exe", hashlib.sha256(data).hexdigest(), tmp_path / "f.exe", opener=opener_for(data)
        )
    assert ei.value.code == "NS-E113" and "disappeared" in ei.value.detail


# ---- unpack_ffmpeg --------------------------------------------------------------------------------
def make_ffmpeg_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def test_unpack_ffmpeg_writes_the_two_programs_from_the_bin_folder_and_nothing_else(tmp_path: Path) -> None:
    root = "ffmpeg-n8.1.3-27-g8dca1586f4-win64-gpl-8.1"
    archive = make_ffmpeg_zip(tmp_path / "f.zip", {
        f"{root}/bin/ffmpeg.exe": b"MZ1", f"{root}/bin/ffprobe.exe": b"MZ2", f"{root}/bin/ffplay.exe": b"MZ3",
        f"{root}/LICENSE.txt": b"gpl", f"{root}/doc/ffmpeg.exe": b"not in bin",
    })
    written = bootstrap.unpack_ffmpeg(archive, tmp_path / "bin" / "ffmpeg")
    assert sorted(written) == ["ffmpeg.exe", "ffprobe.exe"]
    assert sorted(p.name for p in (tmp_path / "bin" / "ffmpeg").iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]
    assert (tmp_path / "bin" / "ffmpeg" / "ffmpeg.exe").read_bytes() == b"MZ1"
    assert bootstrap.ffmpeg_unpacked(tmp_path / "bin" / "ffmpeg")


def test_unpack_ffmpeg_without_ffprobe_is_ns_e110_and_a_bad_zip_too(tmp_path: Path) -> None:
    archive = make_ffmpeg_zip(tmp_path / "f.zip", {"x/bin/ffmpeg.exe": b"MZ"})
    with pytest.raises(BootstrapError) as ei:
        bootstrap.unpack_ffmpeg(archive, tmp_path / "out")
    assert ei.value.code == "NS-E110" and "ffprobe.exe" in ei.value.detail
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(BootstrapError) as ei2:
        bootstrap.unpack_ffmpeg(bad, tmp_path / "out2")
    assert ei2.value.code == "NS-E110"


# ---- B01Tools -------------------------------------------------------------------------------------
class Machine:
    """A PC for b01: which commands exist, whether the runtime is installed, what the installers do."""

    def __init__(self, tmp_path: Path, *, winget: bool, vc: bool = False, ffmpeg_on_path: bool = False,
                 winget_installs: bool = True, elevated_code: int = 0, pins: bool = True,
                 fetch_error: BootstrapError | None = None) -> None:
        self.home = tmp_path / "home"
        self.present = {"wt.exe"} | ({"winget.exe"} if winget else set()) | (
            {"ffmpeg.exe"} if ffmpeg_on_path else set())
        self.vc = vc
        self.winget_installs = winget_installs
        self.elevated_code = elevated_code
        self.fetch_error = fetch_error
        self.calls: list[list[str]] = []
        self.fetched: list[tuple[str, str, Path]] = []
        self.elevated: list[tuple[str, list[str]]] = []
        self.path_adds: list[str] = []
        self.rows: dict[str, tuple[str, str | None]] = {}
        self.said: list[str] = []
        self.boot = Boot(
            app_path=tmp_path / "app", home=self.home, uv="uv", cmd=self.cmd, which=self.which,
            refresh_path=lambda: None, on_row=lambda k, s, n: self.rows.__setitem__(k, (s, n)),
            say=self.said.append, bin_pins=PINS if pins else {}, vc_missing=self.vc_missing,
            fetch_file=self.fetch, run_elevated=self.run_elevated, add_user_path=self.add_path,
        )

    def which(self, name: str) -> str | None:
        return f"C:\\fake\\{name}" if name.lower() in self.present else None

    def vc_missing(self) -> list[str]:
        return [] if self.vc else list(vcruntime.REQUIRED_DLLS)

    def cmd(
        self, argv: Sequence[str], env: Any = None, cwd: Any = None, timeout: float | None = None
    ) -> CmdResult:
        args = [str(a) for a in argv]
        self.calls.append(args)
        if args[0].lower().startswith("winget") and "install" in args:
            if self.winget_installs:
                winget_id = args[args.index("--id") + 1]
                if winget_id == vcruntime.WINGET_ID:
                    self.vc = True
                elif winget_id == "Gyan.FFmpeg":
                    self.present.add("ffmpeg.exe")
                elif winget_id == "Microsoft.WindowsTerminal":
                    self.present.add("wt.exe")
                return CmdResult(0, "installed", "")
            return CmdResult(1, "", "Failed when opening source(s); try the 'source reset' command")
        return CmdResult(0, "", "")

    def fetch(self, url: str, sha256: str, dest: Path, say: Any = None) -> None:
        if self.fetch_error is not None:
            raise self.fetch_error
        self.fetched.append((url, sha256, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix == ".zip":
            make_ffmpeg_zip(dest, {"b/bin/ffmpeg.exe": b"MZ", "b/bin/ffprobe.exe": b"MZ"})
        else:
            dest.write_bytes(b"MZ")

    def run_elevated(self, exe: Path, args: Sequence[str]) -> CmdResult:
        self.elevated.append((str(exe), [str(a) for a in args]))
        if self.elevated_code in bootstrap.VCREDIST_OK_CODES:
            self.vc = True
        return CmdResult(self.elevated_code, "", "")

    def add_path(self, directory: str) -> bool:
        self.path_adds.append(directory)
        return True

    def run(self) -> None:
        step = bootstrap.B01Tools()
        if not step.check(self.boot):
            step.run(self.boot)
        step.verify(self.boot)


def test_git_is_not_one_of_the_tools_and_winget_never_asks_for_it(tmp_path: Path) -> None:
    assert [t[0] for t in bootstrap.TOOLS] == ["vcruntime", "ffmpeg", "terminal"]
    assert not any("git" in t[1].lower() for t in bootstrap.TOOLS)
    m = Machine(tmp_path, winget=True)
    m.run()
    ids = [c[c.index("--id") + 1] for c in m.calls if c[0].lower().startswith("winget")]
    assert ids == [vcruntime.WINGET_ID, "Gyan.FFmpeg"]            # wt.exe was already there
    assert all(c[c.index("--source") + 1] == "winget" for c in m.calls)
    assert m.fetched == [] and m.elevated == []


def test_a_pc_with_everything_installs_nothing(tmp_path: Path) -> None:
    m = Machine(tmp_path, winget=True, vc=True, ffmpeg_on_path=True)
    step = bootstrap.B01Tools()
    assert step.check(m.boot)
    step.run(m.boot)
    assert m.calls == [] and m.rows["vcruntime"] == ("done", "already here")
    assert m.rows["ffmpeg"] == ("done", "already here")


def test_winget_refusing_falls_back_to_the_pinned_downloads(tmp_path: Path) -> None:
    m = Machine(tmp_path, winget=True, winget_installs=False)
    m.run()
    assert [u for u, _s, _d in m.fetched] == [PINS["vcredist"]["url"], PINS["ffmpeg"]["url"]]
    assert m.rows["vcruntime"] == ("done", "installed from the pinned download")
    assert m.rows["ffmpeg"] == ("done", "installed from the pinned download")
    assert m.rows["terminal"] == ("done", "already here")


def test_the_runtime_installer_runs_elevated_with_the_silent_switches(tmp_path: Path) -> None:
    m = Machine(tmp_path, winget=False, ffmpeg_on_path=True)
    m.run()
    (url, digest, dest), = m.fetched
    assert (url, digest) == (PINS["vcredist"]["url"], PINS["vcredist"]["sha256"])
    assert dest == m.home / "bin" / "_downloads" / "vc_redist.x64.exe"
    assert m.elevated == [(str(dest), ["/install", "/quiet", "/norestart"])]
    assert not dest.exists()                                       # the installer is removed afterwards


@pytest.mark.parametrize("code", [0, 1638, 1641, 3010])
def test_runtime_installer_exit_codes_that_mean_installed(tmp_path: Path, code: int) -> None:
    m = Machine(tmp_path, winget=False, ffmpeg_on_path=True, elevated_code=code)
    m.run()
    assert m.rows["vcruntime"][0] == "done"


def test_a_declined_permission_prompt_is_ns_e114_with_the_reason(tmp_path: Path) -> None:
    m = Machine(tmp_path, winget=False, ffmpeg_on_path=True, elevated_code=1223)
    with pytest.raises(BootstrapError) as ei:
        m.run()
    assert ei.value.code == "NS-E114" and "declined" in ei.value.detail and "1223" in ei.value.detail
    assert m.rows["vcruntime"][0] == "failed"


def test_a_failing_runtime_installer_is_ns_e114(tmp_path: Path) -> None:
    m = Machine(tmp_path, winget=False, ffmpeg_on_path=True, elevated_code=5100)
    with pytest.raises(BootstrapError) as ei:
        m.run()
    assert ei.value.code == "NS-E114" and "5100" in ei.value.detail


def test_without_a_pinned_runtime_download_the_failure_is_ns_e114(tmp_path: Path) -> None:
    m = Machine(tmp_path, winget=False, ffmpeg_on_path=True, pins=False)
    with pytest.raises(BootstrapError) as ei:
        m.run()
    assert ei.value.code == "NS-E114"


def test_a_scanner_blocking_the_download_keeps_its_antivirus_code(tmp_path: Path) -> None:
    blocked = BootstrapError("NS-E113", "vc_redist.x64.exe disappeared")
    m = Machine(tmp_path, winget=False, fetch_error=blocked)
    with pytest.raises(BootstrapError) as ei:
        m.run()
    assert ei.value.code == "NS-E113"


def test_ffmpeg_fallback_unpacks_into_the_install_folder_and_joins_the_user_path(tmp_path: Path) -> None:
    m = Machine(tmp_path, winget=False, vc=True)
    m.run()
    folder = m.home / "bin" / "ffmpeg"
    assert (folder / "ffmpeg.exe").is_file() and (folder / "ffprobe.exe").is_file()
    assert m.path_adds == [str(folder)]
    assert [u for u, _s, _d in m.fetched] == [PINS["ffmpeg"]["url"]]
    assert not list((m.home / "bin" / "_downloads").glob("*.zip"))        # the 190 MB archive is deleted
    assert m.rows["ffmpeg"] == ("done", "installed from the pinned download")
    assert bootstrap.B01Tools.have(m.boot, "ffmpeg")


def test_ffmpeg_fallback_runs_after_a_winget_that_installed_nothing(tmp_path: Path) -> None:
    m = Machine(tmp_path, winget=True, vc=True, winget_installs=False)
    m.run()
    winget_calls = [c for c in m.calls if c[0].lower().startswith("winget")]
    assert any("Gyan.FFmpeg" in c for c in winget_calls)
    assert (m.home / "bin" / "ffmpeg" / "ffmpeg.exe").is_file()


def test_a_rerun_after_the_fallback_finds_ffmpeg_in_the_install_folder(tmp_path: Path) -> None:
    m = Machine(tmp_path, winget=False, vc=True)
    m.run()
    m.fetched.clear()
    m.calls.clear()
    step = bootstrap.B01Tools()
    assert step.have(m.boot, "ffmpeg") and step.have(m.boot, "vcruntime")
    m.run()
    assert m.fetched == [] and m.calls == []


# ---- the elevated runner --------------------------------------------------------------------------
def test_run_elevated_exe_asks_windows_for_administrator_rights(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_cmd(
        argv: Sequence[str], env: Any = None, cwd: Any = None, timeout: float | None = None
    ) -> CmdResult:
        seen.append(list(argv))
        return CmdResult(3010, "", "")

    monkeypatch.setattr(bootstrap, "real_cmd", fake_cmd)
    exe = Path("C:/Users/O'Neil/dl/vc_redist.x64.exe")
    res = bootstrap.run_elevated_exe(exe, ["/install", "/quiet", "/norestart"])
    assert res.returncode == 3010
    script = seen[0][-1]
    assert seen[0][0] == "powershell.exe" and "-Verb RunAs" in script and "-Wait" in script
    assert "'/install','/quiet','/norestart'" in script
    assert "O''Neil" in script                                   # a quote in the path cannot end the string
    assert "exit 1223" in script                                 # a declined prompt has a code of its own


# ---- where the programs look ----------------------------------------------------------------------
def test_ffmpeg_module_falls_back_to_the_build_under_the_install_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "bin" / "ffmpeg"
    folder.mkdir(parents=True)
    (folder / "ffmpeg.exe").write_bytes(b"MZ")
    (folder / "ffprobe.exe").write_bytes(b"MZ")
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    monkeypatch.delenv("NS_FFMPEG", raising=False)
    monkeypatch.delenv("NS_FFPROBE", raising=False)
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)         # nothing on PATH
    assert ffmpeg.available()
    assert ffmpeg.ffmpeg_exe() == str(folder / "ffmpeg.exe")
    assert ffmpeg.ffprobe_exe() == str(folder / "ffprobe.exe")
    monkeypatch.setenv("NS_FFMPEG", "D:/mine/ffmpeg.exe")                  # the override still wins
    assert ffmpeg.ffmpeg_exe() == "D:/mine/ffmpeg.exe"


def test_ffmpeg_module_still_says_ns_e401_when_there_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    monkeypatch.delenv("NS_FFMPEG", raising=False)
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)
    assert not ffmpeg.available()
    with pytest.raises(NsError) as ei:
        ffmpeg.ffmpeg_exe()
    assert ei.value.code == "NS-E401"


# ---- the smoke test -------------------------------------------------------------------------------
def test_the_smoke_test_stops_with_ns_e114_before_starting_llama_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from installer.stage2.testing import example_answers, make_test_context

    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, simulate=False)
    monkeypatch.setattr(s08_smoke.vcruntime, "missing", lambda directory=None: ["msvcp140.dll"])
    with pytest.raises(NsError) as ei:
        s08_smoke.check_vc_runtime(ctx)
    assert ei.value.code == "NS-E114" and "msvcp140.dll" in ei.value.detail
    monkeypatch.setattr(s08_smoke.vcruntime, "missing", lambda directory=None: [])
    s08_smoke.check_vc_runtime(ctx)                                        # present: no complaint


def test_the_smoke_test_does_not_judge_the_target_pc_in_a_simulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from installer.stage2.testing import example_answers, make_test_context

    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, simulate=True)
    monkeypatch.setattr(s08_smoke.vcruntime, "missing", lambda directory=None: ["msvcp140.dll"])
    s08_smoke.check_vc_runtime(ctx)


def test_ns_e114_is_defined_with_a_hint_and_operator_copy() -> None:
    errors = json.loads((ROOT / "contracts" / "errors.json").read_text(encoding="utf-8"))
    assert "Visual C++" in errors["NS-E114"]["message"] and "vc_redist.x64.exe" in errors["NS-E114"]["hint"]
    copy = json.loads((ROOT / "installer" / "stage2" / "web" / "operator_copy.json").read_text("utf-8"))
    assert copy["NS-E114"]["title"] and "Retry" in copy["NS-E114"]["whatToDo"]
