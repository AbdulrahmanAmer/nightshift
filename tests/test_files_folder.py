"""D-17: setup takes the pinned programs from a ``files`` folder beside INSTALL.cmd when GitHub is down.

Every test aims the pinned URLs at a fake that answers 504 (or refuses), so any use of the network shows up in
the list of requests. The programs are small stand-in files with their own SHA256 in the manifest.
"""

from __future__ import annotations

import errno
import hashlib
import http.server
import inspect
import io
import json
import os
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from installer.stage2.downloader import is_done, plan_bins, qwentts_item
from installer.stage2.steps import S02DownloadBins, S04Venvs
from installer.stage2.steps.s02_download_bins import QWENTTS_RELEASE_URL
from installer.stage2.testing import FakeRunner, FakeUi, NullReporter, example_answers, make_test_context
from installer.stage2.web import appcopy, bootstrap, engine
from installer.stage2.web.bootstrap import Boot, BootstrapError, CmdResult
from ns_common.config import load_manifest
from ns_common.errors import NsError
from ns_common.models import BinPin

ROOT = Path(__file__).resolve().parents[1]
STAGE1 = ROOT / "installer" / "stage1.ps1"
KIT = ROOT / "tools" / "make-files-kit.ps1"
PS = shutil.which("powershell")
needs_ps = pytest.mark.skipif(PS is None, reason="Windows PowerShell 5.1 is not available")
MISMATCH = "does not match its pin; downloading it"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def leaf(url: str) -> str:
    return Path(urlsplit(url).path).name


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def programs() -> dict[str, bytes]:
    """Stand-ins for what s02 installs on the sample PC (CUDA 13, Nightshift Code chosen)."""
    wv = {"lib/net462/Microsoft.Web.WebView2.Core.dll": b"core",
          "lib/net462/Microsoft.Web.WebView2.Wpf.dll": b"wpf",
          "runtimes/win-x64/native/WebView2Loader.dll": b"loader"}
    return {
        # over two copy blocks (1 MiB each), so a copy can be stopped halfway
        "llama_cuda13": zip_bytes({"llama-server.exe": b"MZ llama", "pad.bin": os.urandom(2_600_000)}),
        "cudart13": zip_bytes({"cudart64_13.dll": b"cudart"}),
        "opencode": zip_bytes({"opencode.exe": b"MZ opencode"}),
        "yt_dlp": b"MZ yt-dlp stand-in",
        "webview2": zip_bytes(wv),
        "qwentts": zip_bytes({"qwen-tts.exe": b"MZ tts", "qwen-codec.exe": b"MZ codec"}),
    }


class RecReporter(NullReporter):
    def __init__(self, ui: RecUi) -> None:
        self.ui = ui

    def update(self, name: str, done: int, total: int | None) -> None:
        if name == self.ui.kill_at_half and total and done < total:
            raise KeyboardInterrupt        # Ctrl+C (or Stop) while the copy is halfway

    def finish(self, name: str, outcome: str) -> None:
        self.ui.outcomes[name] = outcome


class RecUi(FakeUi):
    """Records how each file ended; ``kill_at_half`` names the file whose copy is interrupted."""

    def __init__(self) -> None:
        super().__init__()
        self.outcomes: dict[str, str] = {}
        self.kill_at_half: str | None = None

    def reporter(self, total_bytes: int = 0) -> Any:
        return RecReporter(self)


def github_down(
    log: list[tuple[str, str]], serve: dict[str, bytes] | None = None, ok_head: tuple[str, ...] = (),
):
    """An ``http_factory`` whose every answer is 504, except the URLs in ``serve`` (their bytes) and
    ``ok_head`` (200 to HEAD). Every request is written to ``log``."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        log.append((request.method, url))
        if serve is not None and url in serve:
            data = serve[url]
            if request.method == "HEAD":
                return httpx.Response(200, headers={"content-length": str(len(data))})
            return httpx.Response(200, content=data)
        if request.method == "HEAD" and url in ok_head:
            return httpx.Response(200)
        return httpx.Response(504)

    return lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.fixture
def kit(tmp_path: Path) -> SimpleNamespace:
    """A bundle folder with ``files`` holding every program, and a manifest that pins their real SHA256."""
    base = load_manifest()
    data = programs()
    bins = dict(base.bins)
    app, files = tmp_path / "bundle", tmp_path / "bundle" / "files"
    files.mkdir(parents=True)
    urls: dict[str, str] = {}
    for key, blob in data.items():
        pin = base.bins[key]
        assert pin is not None
        bins[key] = pin.model_copy(update={"sha256": sha(blob)})
        urls[key] = pin.url
        (files / leaf(pin.url)).write_bytes(blob)
    return SimpleNamespace(app=app, files=files, data=data, urls=urls, tmp=tmp_path,
                           manifest=base.model_copy(update={"bins": bins}))


def context(kit: SimpleNamespace, log: list[tuple[str, str]], *, files: Path | None, **http: Any):
    ctx = make_test_context(
        kit.tmp, raw_answers=example_answers(), resolve=True, simulate=False, app_path=kit.app,
        manifest=kit.manifest, ui=RecUi(), http=github_down(log, **http),
    )
    ctx.files_dir = files       # what wizard.build_context sets from --files-dir or <app>/files
    return ctx


def run_s02(ctx: Any) -> BaseException | None:
    """Run the step as the wizard does; the error it raised (None when it finished)."""
    try:
        S02DownloadBins().run(ctx)
    except (NsError, KeyboardInterrupt) as e:
        return e
    return None


def items_of(ctx: Any) -> dict[str, Any]:
    found = {i.name: i for i in plan_bins(ctx.manifest, ctx.profile, ctx.paths.home, include_code=True,
                                          llama_pair="cuda13")}
    voice = qwentts_item(ctx.manifest, ctx.paths.home)
    assert voice is not None
    return {**found, voice.name: voice}


def log_text(ctx: Any) -> str:
    logs = [p for p in ctx.paths.logs.rglob("*") if p.is_file()]
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in logs)


# ------------------------------------------------------------------------------------------------ s02
def test_s02_installs_every_pinned_program_from_the_files_folder_with_github_down(kit):
    log: list[tuple[str, str]] = []
    ctx = context(kit, log, files=kit.files)
    error = run_s02(ctx)
    assert log == [], "setup used the network although every pinned program is in the files folder"
    assert error is None
    items = items_of(ctx)
    assert ctx.ui.outcomes == dict.fromkeys(items, "copied")
    assert all(is_done(i) for i in items.values())
    assert (ctx.paths.bin_llama / "llama-server.exe").read_bytes() == b"MZ llama"
    assert (ctx.paths.home / "bin" / "yt-dlp" / "yt-dlp.exe").read_bytes() == kit.data["yt_dlp"]
    assert (ctx.paths.home / "bin" / "qwentts" / "qwen-tts.exe").is_file() and ctx.flag("qwentts") == "binary"
    # every program's source is said once, by the file it came from
    for key in items:
        said = [line for line in ctx.ui.said if line.startswith(f"{key}: taken from files\\")]
        assert len(said) == 1 and leaf(kit.urls[key]) in said[0], (key, ctx.ui.said)
    assert not list((ctx.paths.home / "bin").rglob("*.copy"))
    # second run: everything is there, nothing is copied or fetched again
    ctx.ui.outcomes.clear()
    assert S02DownloadBins().check(ctx) is True
    assert run_s02(ctx) is None and log == []
    assert ctx.ui.outcomes == dict.fromkeys(items, "skipped")


def test_a_file_with_the_wrong_sha256_is_skipped_and_said(kit):
    name = leaf(kit.urls["llama_cuda13"])
    (kit.files / name).write_bytes(kit.data["llama_cuda13"][:-1] + b"X")       # one bad file among good ones
    log: list[tuple[str, str]] = []
    ctx = context(kit, log, files=kit.files)
    error = run_s02(ctx)
    line = f"files\\{name} {MISMATCH}"
    assert ctx.ui.said.count(line) == 1, ctx.ui.said                           # the console (or the window)
    assert log_text(ctx).count(MISMATCH) == 1                                  # and the log, once each
    items = items_of(ctx)
    assert not is_done(items["llama_cuda13"]) and ctx.ui.outcomes["llama_cuda13"] == "failed"
    assert not (ctx.paths.bin_llama / "llama-server.exe").exists()             # the wrong bytes: never used
    assert {k: v for k, v in ctx.ui.outcomes.items() if k != "llama_cuda13"} == {
        "cudart13": "copied", "opencode": "copied", "yt_dlp": "copied", "webview2": "copied"}
    assert {url for _m, url in log} == {kit.urls["llama_cuda13"]}              # only the bad file went online
    assert isinstance(error, NsError) and error.code == "NS-E105"
    assert "504" in error.detail                                               # today's words stay
    assert "files folder" in error.detail and "tools\\make-files-kit.ps1" in error.detail
    assert "next to INSTALL.cmd" in error.detail and "run INSTALL.cmd again" in error.detail
    # failed, then retried: the owner puts the right file there and runs setup again
    (kit.files / name).write_bytes(kit.data["llama_cuda13"])
    log.clear()
    assert run_s02(ctx) is None and log == []
    assert ctx.ui.outcomes["llama_cuda13"] == "copied" and all(is_done(i) for i in items_of(ctx).values())


def test_a_copy_killed_halfway_leaves_no_file_that_counts_as_done(kit):
    log: list[tuple[str, str]] = []
    ctx = context(kit, log, files=kit.files)
    ctx.ui.kill_at_half = "llama_cuda13"
    error = run_s02(ctx)
    assert isinstance(error, KeyboardInterrupt), repr(error)
    item = items_of(ctx)["llama_cuda13"]
    assert not list((ctx.paths.home / "bin").rglob("*.copy")) and not item.dest.exists()
    assert not is_done(item) and S02DownloadBins().check(ctx) is False
    # a console that was killed outright cannot clean up: the half copy it leaves counts for nothing either
    item.dest.with_name(item.dest.name + ".copy").write_bytes(kit.data["llama_cuda13"][:1000])
    assert not is_done(item) and S02DownloadBins().check(ctx) is False
    ctx.ui.kill_at_half = None                                                 # the next run copies it again
    assert run_s02(ctx) is None and log == []
    assert ctx.ui.outcomes["llama_cuda13"] == "copied" and is_done(item)
    assert not list((ctx.paths.home / "bin").rglob("*.copy"))


def test_a_full_disk_during_the_copy_is_ns_e104_and_not_a_download(kit, monkeypatch):
    real, disk = os.replace, {"full": True}

    def replace(src, dst):
        if disk["full"] and str(src).endswith(".copy"):
            raise OSError(errno.ENOSPC, "No space left on device")
        real(src, dst)

    monkeypatch.setattr(os, "replace", replace)             # the rename that ends every copy
    log: list[tuple[str, str]] = []
    ctx = context(kit, log, files=kit.files)
    error = run_s02(ctx)
    assert log == []                                        # a full disk is not answered with a download
    assert isinstance(error, NsError) and error.code == "NS-E104", repr(error)
    assert not list((ctx.paths.home / "bin").rglob("*.copy"))
    disk["full"] = False                                    # space was freed: Retry copies everything
    assert run_s02(ctx) is None and log == [] and all(is_done(i) for i in items_of(ctx).values())


def test_a_program_removed_right_after_unpacking_a_copied_zip_is_ns_e113(kit, monkeypatch):
    """The copied zip is unpacked by the same code as a downloaded one: a scanner that deletes the program
    a moment later is the antivirus code, as before."""
    from installer.stage2 import downloader

    real = downloader.extract_zip

    def unpack_then_scan(zip_path, target):
        names = real(zip_path, target)
        (Path(target) / "llama-server.exe").unlink(missing_ok=True)
        return names

    monkeypatch.setattr(downloader, "extract_zip", unpack_then_scan)
    log: list[tuple[str, str]] = []
    ctx = context(kit, log, files=kit.files)
    error = run_s02(ctx)
    assert log == []
    assert isinstance(error, NsError) and error.code == "NS-E113", repr(error)
    assert "llama-server.exe" in error.detail and not is_done(items_of(ctx)["llama_cuda13"])


def test_an_unpinned_voice_engine_zip_is_never_taken_from_the_files_folder(kit):
    """The ``latest`` probe has no SHA256 to check, so a zip of that name in ``files`` is not used."""
    kit.manifest = kit.manifest.model_copy(update={"bins": {**kit.manifest.bins, "qwentts": None}})
    for name in (leaf(QWENTTS_RELEASE_URL), "qwentts.zip"):
        (kit.files / name).write_bytes(kit.data["qwentts"])
    log: list[tuple[str, str]] = []
    ctx = context(kit, log, files=kit.files, ok_head=(QWENTTS_RELEASE_URL,))
    error = run_s02(ctx)
    assert ("GET", QWENTTS_RELEASE_URL) in log                                 # it went to the network
    assert not (ctx.paths.home / "bin" / "qwentts" / "qwen-tts.exe").exists()
    assert isinstance(error, NsError) and error.code == "NS-E105"
    assert "make-files-kit" not in error.detail             # the kit cannot hold it, so the hint is not given


@pytest.mark.parametrize("folder", ["absent", "empty"])
def test_no_files_folder_changes_nothing(kit, folder):
    """Control (a bundle of the previous release has no ``files``): the requests the base makes, per file in
    the same order, the same outcomes and not a word about a files folder."""
    shutil.rmtree(kit.files)
    if folder == "empty":
        kit.files.mkdir()
    serve = {kit.urls[k]: v for k, v in kit.data.items()}
    log: list[tuple[str, str]] = []
    ctx = context(kit, log, files=kit.files if folder == "empty" else None, serve=serve)
    assert run_s02(ctx) is None
    per_url: dict[str, list[str]] = {}
    for method, url in log:
        per_url.setdefault(url, []).append(method)
    assert per_url == {url: ["HEAD", "GET"] for url in serve}
    assert ctx.ui.outcomes == dict.fromkeys(items_of(ctx), "downloaded")
    for words in ("taken from", "does not match its pin", "files folder", "files_folder"):
        assert words not in ctx.ui.text and words not in log_text(ctx)
    # the wizard's command line is the one of the previous release
    argv = engine.wizard_argv("py", app_path=kit.app, home=kit.tmp, answers=kit.tmp / "a.json",
                              preflight=kit.tmp / "p.json", bridge=True)
    assert "--files-dir" not in argv


def test_the_setup_window_hands_the_files_folder_to_the_wizard(kit, monkeypatch):
    """Stage 2 runs from ``<home>/app``, which never holds ``files``: the bootstrap names the folder."""
    from installer.stage2 import wizard
    from installer.stage2.web import controller
    from tests.test_installer_web_controller import Setup, finish_script

    assert "files_dir" in inspect.signature(engine.wizard_argv).parameters
    # the window's route, on fakes: the folder beside the bundle reaches b01 and the wizard's command line
    monkeypatch.setattr(controller, "files_beside", lambda app: kit.files if Path(app) == ROOT else None)
    s = Setup(kit.tmp, monkeypatch, script=finish_script)
    assert s.ctl._boot().files_dir == kit.files
    s.accept()
    assert s.ctl.do_action("start")[0] == 200
    s.until(lambda st: st["run"] == "finished", what="the run to finish")
    argv = s.engine.argv
    assert argv[argv.index("--files-dir") + 1] == str(kit.files)
    # the wizard reads it; run from the bundle (the console) it finds the folder by itself; a folder that is
    # gone is no folder
    args = wizard.parse_args(argv[5:])
    assert wizard.files_dir_of(args, kit.tmp / "home" / "app") == kit.files
    assert wizard.files_dir_of(wizard.parse_args([]), kit.app) == kit.files
    assert wizard.files_dir_of(wizard.parse_args(["--files-dir", str(kit.tmp / "gone")]), kit.tmp) is None


# ------------------------------------------------------------------------------------------ bootstrap
def tools_boot(kit: SimpleNamespace, files: Path | None, *, fetch_ok: bool = True) -> SimpleNamespace:
    """``Boot`` for b01 on a PC with winget and Windows Terminal but no FFmpeg and no Visual C++ runtime.
    Every outside effect is a recorder here (no registry, no PATH, no elevation, no network)."""
    ffmpeg = zip_bytes({"ffmpeg-n8/bin/ffmpeg.exe": b"MZ ffmpeg", "ffmpeg-n8/bin/ffprobe.exe": b"MZ ffprobe"})
    vc = b"MZ vc_redist stand-in"
    pins = {"ffmpeg": {"url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/x/ffmpeg-n8-win64.zip",
                       "sha256": sha(ffmpeg)},
            "vcredist": {"url": "https://download.visualstudio.microsoft.com/pr/abc/VC_redist.x64.exe",
                         "sha256": sha(vc)}}
    rec = SimpleNamespace(cmds=[], fetched=[], elevated=[], rows=[], said=[], path=[], ffmpeg=ffmpeg, vc=vc,
                          pins=pins, runtime=False)

    def cmd(argv, env=None, cwd=None, timeout=None):
        rec.cmds.append([str(a) for a in argv])
        return CmdResult(1, "", "winget: the source does not answer")

    def fetch(url, sha256, dest, say=None):
        rec.fetched.append(url)
        if not fetch_ok:
            raise BootstrapError("NS-E105", f"could not download {url}: HTTP Error 504: Gateway Time-out")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(ffmpeg if url == pins["ffmpeg"]["url"] else vc)

    def elevated(exe, args):
        rec.elevated.append(Path(exe).read_bytes())
        rec.runtime = True
        return CmdResult(0, "", "")

    rec.boot = Boot(
        app_path=kit.app, home=kit.tmp / "home", uv="uv", cmd=cmd,
        which=lambda name: name if name in ("winget.exe", "wt.exe") else None, refresh_path=lambda: None,
        set_home_var=lambda n, v: True, get_home_var=lambda n: None, bin_pins=pins,
        vc_missing=lambda: [] if rec.runtime else ["vcruntime140.dll"], fetch_file=fetch,
        run_elevated=elevated,
        add_user_path=lambda d: rec.path.append(d) or True, on_row=lambda *row: rec.rows.append(row),
        say=rec.said.append,
    )
    rec.boot.files_dir = files      # what the controller sets from the bundle folder
    return rec


def test_ffmpeg_and_the_vc_runtime_come_from_the_files_folder_before_winget(kit):
    rec = tools_boot(kit, kit.files)
    (kit.files / "ffmpeg-n8-win64.zip").write_bytes(rec.ffmpeg)
    (kit.files / "VC_redist.x64.exe").write_bytes(rec.vc)
    bootstrap.B01Tools().run(rec.boot)
    assert not [c for c in rec.cmds if "winget" in c[0].lower()], "winget ran although files had the pin"
    assert rec.fetched == []
    assert rec.elevated == [rec.vc]                                 # the installer that ran is the pinned one
    assert (rec.boot.ffmpeg_dir / "ffmpeg.exe").read_bytes() == b"MZ ffmpeg"
    assert (rec.boot.ffmpeg_dir / "ffprobe.exe").is_file()
    notes = {row[0]: row[2] for row in rec.rows if row[1] == "done"}
    assert notes["vcruntime"] == notes["ffmpeg"] == "installed from the files folder"
    for name in ("VC_redist.x64.exe", "ffmpeg-n8-win64.zip"):
        assert sum(f"taken from files\\{name}" in line for line in rec.said) == 1, rec.said
    assert not list(rec.boot.downloads_dir.glob("*.copy"))


def test_a_wrong_file_in_the_files_folder_goes_on_to_winget_and_the_pinned_download(kit):
    rec = tools_boot(kit, kit.files)
    (kit.files / "ffmpeg-n8-win64.zip").write_bytes(rec.ffmpeg + b"tail")
    (kit.files / "VC_redist.x64.exe").write_bytes(rec.vc)
    bootstrap.B01Tools().run(rec.boot)
    wrong = "files\\ffmpeg-n8-win64.zip does not match its pin"
    assert sum(wrong in line for line in rec.said) == 1, rec.said
    assert [c[3] for c in rec.cmds if "winget" in c[0].lower()] == ["Gyan.FFmpeg"]     # the next source
    assert rec.fetched == [rec.pins["ffmpeg"]["url"]]                                  # then the pinned URL
    assert (rec.boot.ffmpeg_dir / "ffmpeg.exe").read_bytes() == b"MZ ffmpeg"


def test_a_failed_bootstrap_download_names_the_files_folder_and_the_kit(kit):
    rec = tools_boot(kit, None, fetch_ok=False)
    with pytest.raises(BootstrapError) as ei:
        bootstrap.B01Tools().run(rec.boot)
    assert ei.value.code == "NS-E105" and "504" in ei.value.detail
    assert "files folder" in ei.value.detail and "tools\\make-files-kit.ps1" in ei.value.detail
    assert "next to INSTALL.cmd" in ei.value.detail


# ------------------------------------------------------------------------------------------------ s04
def test_the_spacy_wheel_is_installed_from_the_files_folder_with_its_hash(kit):
    wheel_bytes = zip_bytes({"en_core_web_md/__init__.py": b""})
    name = "en_core_web_md-3.8.0-py3-none-any.whl"
    pin = BinPin(version="3.8.0", url=f"https://github.com/explosion/spacy-models/releases/download/x/{name}",
                 sha256=sha(wheel_bytes))

    def spacy_line(files: Path | None, sub: str) -> tuple[str, Any, list[str]]:
        runner = FakeRunner(kit.tmp / sub / "desktop")
        ctx = make_test_context(kit.tmp / sub, raw_answers=example_answers(), resolve=True, runner=runner,
                                app_path=ROOT)
        ctx.manifest = ctx.manifest.model_copy(update={"python": ctx.manifest.python.model_copy(
            update={"spacy_wheel": pin})})
        ctx.files_dir = files
        S04Venvs().run(ctx)
        call = next(c for c in runner.calls if c.tag == "uv pip install studio spacy model")
        assert call.argv[-4:-1] == ["--no-deps", "--require-hashes", "-r"]
        return Path(call.argv[-1]).read_text(encoding="utf-8").strip(), ctx, call.argv

    (kit.files / name).write_bytes(wheel_bytes)
    line, ctx, _argv = spacy_line(kit.files, "a")
    assert line == f"en_core_web_md @ {(kit.files / name).as_uri()} --hash=sha256:{pin.sha256}"
    assert sum(f"taken from files\\{name}" in said for said in ctx.ui.said) == 1
    # a wheel with other bytes is not handed to uv: the pinned URL is, with the same hash, and it is said
    (kit.files / name).write_bytes(wheel_bytes + b"x")
    line, ctx, _argv = spacy_line(kit.files, "b")
    assert line == f"en_core_web_md @ {pin.url} --hash=sha256:{pin.sha256}"
    assert ctx.ui.said.count(f"files\\{name} {MISMATCH}") == 1
    # no files folder: the line of the previous release
    assert spacy_line(None, "c")[0] == f"en_core_web_md @ {pin.url} --hash=sha256:{pin.sha256}"


# -------------------------------------------------------------------------------------------- appcopy
def test_the_files_folder_is_never_copied_into_the_program_folder(tmp_path: Path):
    source = tmp_path / "bundle"
    (source / "files" / "python").mkdir(parents=True)
    (source / "files" / "llama.zip").write_bytes(b"big")
    (source / "installer" / "files").mkdir(parents=True)             # only the top-level folder is left out
    (source / "installer" / "files" / "keep.txt").write_text("x", encoding="utf-8")
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    appcopy.copy_tree(source, tmp_path / "app")
    assert "files" in appcopy.SKIP_TOP
    assert not (tmp_path / "app" / "files").exists()
    assert (tmp_path / "app" / "manifest.json").is_file()
    assert (tmp_path / "app" / "installer" / "files" / "keep.txt").is_file()


# -------------------------------------------------------------------------------------------- stage 1
LOADER = """
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{stage1}', [ref]$null, [ref]$null)
$fnType = [System.Management.Automation.Language.FunctionDefinitionAst]
foreach ($name in 'Get-Prop', 'Join-Str', 'Get-KitHint', 'Get-UrlLeaf', 'Get-FilesDir', 'Copy-PinnedFile',
                  'Install-Uv', 'Install-ManagedPython', 'Install-VcRuntime') {{
    $found = @($ast.FindAll({{ param($n) $n -is $fnType -and $n.Name -eq $name }}, $true))
    if ($found.Count -gt 0) {{ . ([scriptblock]::Create($found[0].Extent.Text)) }}
    else {{ Write-Host "MISSING:$name" }}
}}
function Get-DataDir {{ return '{data}' }}
function Set-Status {{ param($Index, $Tag, $Text) Write-Host "STATUS:$Tag|$Text" }}
function Stop-Setup {{ param($Code, $Detail, $ExitCode) Write-Host "STOP:$Code|$Detail"; exit 13 }}
function Invoke-WebRequest {{ Write-Host 'WEB-CALLED'; throw 'HTTP 504 (the test has no network)' }}
function Test-Tool {{ param($Name) return $false }}
function Update-SessionPath {{ }}
function Get-UvVersion {{ param($Uv) return '0.13.6' }}
function Test-VcRuntime {{ return $true }}
function Start-Process {{
    [CmdletBinding()] param($FilePath, $ArgumentList, $Verb, [switch]$Wait, [switch]$PassThru)
    Write-Host ('ELEVATED:' + (Get-FileHash -Algorithm SHA256 -LiteralPath $FilePath).Hash.ToLower())
    return [pscustomobject]@{{ ExitCode = 0 }}
}}
$script:IsDry = $false
$script:UvFrom = 'downloaded'
$m = Get-Content -Raw -LiteralPath '{manifest}' | ConvertFrom-Json
$files = ''
if (Get-Command Get-FilesDir -ErrorAction SilentlyContinue) {{ $files = Get-FilesDir -App '{app}' }}
Write-Host "FILES=[$files]"
"""


def stage1(tmp_path: Path, app: Path, manifest: dict, body: str, env: dict[str, str] | None = None):
    """Run the real functions of stage1.ps1 under powershell.exe with stand-ins for the machine."""
    work = tmp_path / "ps"
    (work / "temp").mkdir(parents=True, exist_ok=True)
    (work / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    script = LOADER.format(stage1=STAGE1, data=work / "data", manifest=work / "manifest.json", app=app) + body
    (work / "harness.ps1").write_text(script, encoding="utf-8")
    full = {k: v for k, v in os.environ.items() if k.upper() != "UV_PYTHON_INSTALL_MIRROR"}
    full.update({"TEMP": str(work / "temp"), "TMP": str(work / "temp"), **(env or {})})
    argv = [str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(work / "harness.ps1")]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=180, env=full)
    return proc, work


def fake_uv(folder: Path, refuse_mirror: bool = False, fail: bool = False) -> Path:
    """A stand-in uv: writes the mirror variable and its arguments to uv-calls.txt and exits 0 (2 when it
    refuses the file of a folder mirror, 3 when it fails anyway)."""
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["@echo off", 'echo MIRROR=[%UV_PYTHON_INSTALL_MIRROR%] ARGS=[%*]>>"%~dp0uv-calls.txt"']
    if refuse_mirror:
        lines.append('if /i "%UV_PYTHON_INSTALL_MIRROR:~0,5%"=="file:" exit /b 2')
    lines.append("exit /b 3" if fail else "exit /b 0")
    (folder / "fakeuv.cmd").write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    return folder / "fakeuv.cmd"


@needs_ps
def test_stage1_takes_uv_and_python_from_the_files_folder(tmp_path: Path):
    app, files = tmp_path / "bundle", tmp_path / "bundle" / "files"
    (files / "python" / "20261026").mkdir(parents=True)
    uv_zip, vc = zip_bytes({"uv.exe": b"MZ uv stand-in", "uvx.exe": b"MZ uvx"}), b"MZ vc_redist stand-in"
    (files / "uv-x86_64-pc-windows-msvc.zip").write_bytes(uv_zip)
    (files / "VC_redist.x64.exe").write_bytes(vc)
    manifest = {"python_version": "3.12", "bins": {
        "uv": {"version": "0.13.6", "url": "http://127.0.0.1:9/dl/0.13.6/uv-x86_64-pc-windows-msvc.zip",
               "sha256": sha(uv_zip)},
        "vcredist": {"version": "14", "url": "http://127.0.0.1:9/pr/VC_redist.x64.exe", "sha256": sha(vc)}}}
    uv = fake_uv(tmp_path / "uv")
    body = f"""
$hasFiles = (Get-Command Install-Uv).Parameters.ContainsKey('FilesDir')
if ($hasFiles) {{ $got = Install-Uv -Manifest $m -FilesDir $files }} else {{ $got = Install-Uv -Manifest $m }}
Write-Host "UV=[$got] FROM=[$script:UvFrom]"
if (Get-Command Install-ManagedPython -ErrorAction SilentlyContinue) {{
    $note = Install-ManagedPython -Uv '{uv}' -Version '3.12' -FilesDir $files
    Write-Host "PY=[$note] AFTER=[$env:UV_PYTHON_INSTALL_MIRROR]"
    $ok = Install-VcRuntime -Manifest $m -FilesDir $files
    Write-Host "VC=[$ok]"
}}
"""
    proc, work = stage1(tmp_path, app, manifest, body)
    out = proc.stdout + proc.stderr
    assert f"FILES=[{files}]" in out, out
    assert f"UV=[{work / 'data' / 'bootstrap' / 'uv' / 'uv.exe'}] FROM=[from the files folder]" in out, out
    assert "WEB-CALLED" not in out and proc.returncode == 0, out                    # no download was tried
    assert (work / "data" / "bootstrap" / "uv" / "uv.exe").read_bytes() == b"MZ uv stand-in"
    assert "taken from files\\uv-x86_64-pc-windows-msvc.zip" in out
    # Python: the mirror is the files\python folder for the one `uv python install` call and gone after it
    calls = (uv.parent / "uv-calls.txt").read_text(encoding="ascii").splitlines()
    assert calls == [f"MIRROR=[{(files / 'python').as_uri()}] ARGS=[python install 3.12]"]
    assert "PY=[from the files folder] AFTER=[]" in out
    # the Visual C++ runtime: the installer that was started is the pinned file from the folder
    assert f"ELEVATED:{sha(vc)}" in out and "VC=[True]" in out
    assert "taken from files\\VC_redist.x64.exe" in out
    assert not [p.name for p in (work / "temp").iterdir()], "stage 1 left a file in the temp folder"


@needs_ps
def test_stage1_goes_on_when_the_files_folder_cannot_be_used(tmp_path: Path):
    """A wrong uv zip is said and not unpacked; a Python archive uv refuses is retried once without the mirror
    (the owner's own mirror setting comes back); when that fails too the error names the kit."""
    app, files = tmp_path / "bundle", tmp_path / "bundle" / "files"
    (files / "python").mkdir(parents=True)
    uv_zip = zip_bytes({"uv.exe": b"MZ uv stand-in"})
    (files / "uv-x86_64-pc-windows-msvc.zip").write_bytes(uv_zip + b"tail")
    manifest = {"python_version": "3.12", "bins": {"uv": {
        "version": "0.13.6", "url": "http://127.0.0.1:9/dl/uv-x86_64-pc-windows-msvc.zip",
        "sha256": sha(uv_zip)}}}
    refusing, failing = fake_uv(tmp_path / "uv1", refuse_mirror=True), fake_uv(tmp_path / "uv2", fail=True)
    mine = "https://mirror.example/python"
    body = f"""
if (Get-Command Install-ManagedPython -ErrorAction SilentlyContinue) {{
    $note = Install-ManagedPython -Uv '{refusing}' -Version '3.12' -FilesDir $files
    Write-Host "PY=[$note] AFTER=[$env:UV_PYTHON_INSTALL_MIRROR]"
    $null = Install-ManagedPython -Uv '{failing}' -Version '3.12' -FilesDir ''
}}
"""
    proc, _work = stage1(tmp_path, app, manifest, body, env={"UV_PYTHON_INSTALL_MIRROR": mine})
    out = proc.stdout + proc.stderr
    assert "MISSING:" not in out, out
    calls = (refusing.parent / "uv-calls.txt").read_text(encoding="ascii").splitlines()
    assert calls == [f"MIRROR=[{(files / 'python').as_uri()}] ARGS=[python install 3.12]",
                     f"MIRROR=[{mine}] ARGS=[python install 3.12]"], out
    assert "files\\python was not accepted by uv (exit code 2); downloading Python 3.12" in out
    assert f"PY=[downloaded] AFTER=[{mine}]" in out
    stop = next(line for line in out.splitlines() if line.startswith("STOP:"))
    assert stop.startswith("STOP:NS-E105|uv python install 3.12 failed (exit code 3).")
    assert proc.returncode == 13
    assert "tools\\make-files-kit.ps1" in stop and "next to INSTALL.cmd" in stop
    # uv: the wrong zip is said once, never unpacked, and the download (dead here) is what fails
    body = """
$got = Install-Uv -Manifest $m -FilesDir $files
"""
    proc, work = stage1(tmp_path / "second", app, manifest, body)
    out = proc.stdout + proc.stderr
    assert out.count(f"files\\uv-x86_64-pc-windows-msvc.zip {MISMATCH}") == 1, out
    assert "WEB-CALLED" in out and "STOP:NS-E105|" in out and "tools\\make-files-kit.ps1" in out
    assert not (work / "data" / "bootstrap" / "uv" / "uv.exe").exists()
    assert not [p.name for p in (work / "temp").iterdir()]


@needs_ps
def test_a_dry_run_of_stage1_names_the_files_folder_and_hands_it_to_every_step(tmp_path: Path):
    """The whole script, as CI runs it (-DryRun with a fake probe): the folder beside INSTALL.cmd is found and
    said, and the three places that fetch a program get it."""
    app = tmp_path / "bundle"
    (app / "files").mkdir(parents=True)
    shutil.copy(ROOT / "manifest.json", app / "manifest.json")
    probe = tmp_path / "probe.json"
    drives = [{"letter": "C", "free_gb": 500, "type": "SSD"}]
    probe.write_text(json.dumps({"smi_text": "CUDA Version: 13.0", "vc_runtime": False, "drives": drives}),
                     encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "NIGHTSHIFT_HOME"}
    env["NS_NONINTERACTIVE"] = "1"

    def dry(folder: Path) -> str:
        argv = [str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(STAGE1), "-AppPath",
                str(folder), "-DryRun", "-FakeProbe", str(probe)]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=300, env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout

    line = f"[dry-run] take the pinned programs from {app / 'files'} when their SHA256 matches"
    assert line in dry(app)
    (app / "files").rmdir()
    without = dry(app)                                              # no folder: not a word about one
    assert "pinned programs from" not in without and "files folder" not in without
    text = STAGE1.read_text(encoding="ascii")
    main = text[text.index("# Main"):]
    for call in ("Install-Uv -Manifest $Manifest -FilesDir $filesDir",
                 "Install-ManagedPython -Uv $uv -Version $pyVersion -FilesDir $filesDir",
                 "Install-VcRuntime -Manifest $Manifest -FilesDir $filesDir"):
        assert call in main, call


# ------------------------------------------------------------------------------------------- the kit
class _Quiet(http.server.SimpleHTTPRequestHandler):
    seen: list[str] = []

    def log_message(self, format: str, *args: Any) -> None:
        self.seen.append(self.path)


@needs_ps
def test_the_kit_script_parses_under_5_1_and_writes_files_json(tmp_path: Path):
    site, out = tmp_path / "site", tmp_path / "kit" / "files"
    (site / "dl").mkdir(parents=True)
    uv_zip, ytdlp = zip_bytes({"uv.exe": b"MZ uv"}), b"MZ yt-dlp stand-in"
    (site / "dl" / "uv-x86_64-pc-windows-msvc.zip").write_bytes(uv_zip)
    (site / "dl" / "yt-dlp.exe").write_bytes(ytdlp)
    _Quiet.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: _Quiet(*a, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/dl"
    dead = "http://127.0.0.1:9/never"

    def manifest(ytdlp_sha: str) -> Path:
        data = {"python_version": "3.12", "python": {"spacy_wheel": {"version": "1", "url": f"{dead}/w.whl",
                                                                      "sha256": "0" * 64}},
                "bins": {"uv": {"version": "0.13.6", "url": f"{base}/uv-x86_64-pc-windows-msvc.zip",
                                "sha256": sha(uv_zip)},
                         "yt_dlp": {"version": "1", "url": f"{base}/yt-dlp.exe", "sha256": ytdlp_sha},
                         "llama_cpu": {"version": "1", "url": f"{dead}/llama.zip", "sha256": "0" * 64},
                         "qwentts": None}}
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def run(ytdlp_sha: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(KIT), "-Manifest",
             str(manifest(ytdlp_sha)), "-Out", str(out), "-Only", "uv,yt-dlp"],
            capture_output=True, text=True, timeout=180)

    try:
        proc = run(sha(ytdlp))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (out / "uv-x86_64-pc-windows-msvc.zip").read_bytes() == uv_zip
        assert (out / "yt-dlp.exe").read_bytes() == ytdlp
        listed = json.loads((out / "files.json").read_text(encoding="utf-8-sig"))["files"]
        assert {(f["key"], f["file"], f["sha256"]) for f in listed} == {
            ("uv", "uv-x86_64-pc-windows-msvc.zip", sha(uv_zip)), ("yt_dlp", "yt-dlp.exe", sha(ytdlp))}
        assert sorted(_Quiet.seen) == ["/dl/uv-x86_64-pc-windows-msvc.zip", "/dl/yt-dlp.exe"]  # -Only is kept
        assert sorted(p.name for p in out.iterdir()) == [
            "files.json", "uv-x86_64-pc-windows-msvc.zip", "yt-dlp.exe"]
        # second run: what is there and matches is not downloaded again
        proc = run(sha(ytdlp))
        assert proc.returncode == 0 and len(_Quiet.seen) == 2, proc.stdout + proc.stderr
        assert proc.stdout.count("already here") == 2
        # a download that does not match its pin is not kept, is said, and the script fails
        (out / "yt-dlp.exe").unlink()
        proc = run("1" * 64)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "yt-dlp.exe does not match its pin" in proc.stdout and not (out / "yt-dlp.exe").exists()
        assert not list(out.glob("*.part"))
        listed = json.loads((out / "files.json").read_text(encoding="utf-8-sig"))["files"]
        assert [f["key"] for f in listed] == ["uv"]                 # files.json lists only what was checked
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
