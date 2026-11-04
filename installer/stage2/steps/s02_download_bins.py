"""s02_download_bins: llama.cpp (+ cudart), OpenCode, yt-dlp; qwentts.cpp when a binary exists (component C1).

The llama.cpp pair follows the driver: CUDA 13.0 or newer takes the CUDA 13.4 build (Blackwell needs it),
CUDA 12.4 to 12.x the CUDA 12.4 build, a version that was read and is older is NS-E103, and a driver whose
version could not be read at all takes the CUDA 12.4 build with a warning (NS-W115). ``bins.qwentts`` pins the
voice engine release asset; when a manifest leaves it null, the latest GitHub release is probed under the new
asset name and then the old one; without a binary the PyTorch fallback is scheduled (NS-W301).
yt-dlp.exe (pinned in ``bins.yt_dlp``) goes to ``bin/yt-dlp/yt-dlp.exe`` for the Clipper link sources.
The WebView2 window files (``bins.webview2``) come from the bundle's ``bin/webview2`` when the SHA256 match.

A dev install (``ctx.dev_mode``, docs/DEV_MODE.md) downloads the CPU llama.cpp build, yt-dlp and the window
files for real and skips cudart, OpenCode and qwentts.cpp; there is no PyTorch fallback either.
"""

from __future__ import annotations

import httpx

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.downloader import (
    DownloadItem,
    adopt_bundled_webview2,
    blocked_error,
    is_done,
    plan_bins,
    qwentts_item,
    release_zip_item,
    removed_after_unpack,
)
from installer.stage2.errors import make_error
from installer.stage2.runtime_cfg import update_runtime
from installer.stage2.sysinfo import is_blackwell, llama_pair_for

QWENTTS_RELEASE_URL = (
    "https://github.com/AbdulrahmanAmer/nightshift/releases/latest/download/"
    "Nightshift-VoiceEngine-win-cuda-x64.zip"
)
# the name before 0.1.3 (v0.1.0 to v0.1.2 releases); tried second when the new name answers 404
QWENTTS_RELEASE_URL_OLD = (
    "https://github.com/AbdulrahmanAmer/nightshift/releases/latest/download/qwentts-win-cuda-x64.zip"
)


def choose_pair(ctx: StepContext, *, warn: bool = False) -> str | None:
    """cuda13 / cuda12 for the GPU profile; None for cpu-dev.

    NS-E103 only when stage 1 read a CUDA version and it is below 12.4. When it could not read one (``cuda``
    empty: nvidia-smi missing or silent and no usable driver number) the CUDA 12.4 build is used and, with
    ``warn``, NS-W115 is recorded: a refusal there would stop a working PC on a missing file.
    """
    if ctx.profile == "cpu-dev":
        return None
    pf = ctx.preflight or {}
    cuda = str(pf.get("cuda") or "")
    gpu_name = str((pf.get("gpu") or {}).get("name") or "")
    blackwell = is_blackwell(gpu_name)
    if not cuda:
        if blackwell:
            # an RTX 50-series card only ships with CUDA 13 drivers, and the 12.4 build cannot run on it
            if warn:
                ctx.warn("NS-W115", "the driver version could not be read; "
                         "using the CUDA 13 build for this RTX 50-series card")
            return "cuda13"
        if warn:
            ctx.warn("NS-W115", "the driver version could not be read; using the CUDA 12.4 build")
        return "cuda12"
    pair = llama_pair_for(cuda)
    if pair is None:
        raise make_error("NS-E103", f"nvidia-smi reports CUDA {cuda or 'unknown'}; 12.4 or newer is required")
    if pair == "cuda12" and blackwell:
        raise make_error(
            "NS-E103",
            f"nvidia-smi reports CUDA {cuda} on {gpu_name}; RTX 50-series cards need a driver with CUDA 13 "
            "(driver 580 or newer), the CUDA 12.4 build cannot run on them",
        )
    return pair


def probe_release(ctx: StepContext, url: str) -> bool:
    """HEAD the qwentts release asset. True on a 2xx answer (redirects followed); False on 404/unreachable."""
    if ctx.simulate and not ctx.head_checks:
        return False                      # pure simulation never touches the network
    client = ctx.http_factory()
    try:
        return client.head(url).status_code < 400
    except httpx.HTTPError as e:
        ctx.log.warn("qwentts_probe_failed", f"{type(e).__name__}")
        return False
    finally:
        client.close()


class S02DownloadBins(Step):
    """Download and unpack the binaries."""

    id = "s02_download_bins"
    title = "Download programs"

    def _items(self, ctx: StepContext) -> list[DownloadItem]:
        answers = ctx.ensure_answers()
        # the release bundle carries the window files: use them, not the nupkg (no-op otherwise)
        adopt_bundled_webview2(ctx.manifest, ctx.app_path, ctx.paths.home)
        return plan_bins(
            ctx.manifest, ctx.profile, ctx.paths.home, include_code=answers.install_code,
            llama_pair=choose_pair(ctx),
        )

    def check(self, ctx: StepContext) -> bool:
        """All zips extracted and the qwentts decision recorded."""
        return all(is_done(i) for i in self._items(ctx)) and ctx.flag("qwentts") is not None

    def run(self, ctx: StepContext) -> None:
        """Download the llama.cpp pair (and OpenCode), then settle the qwentts.cpp question."""
        pair = choose_pair(ctx, warn=True)
        pf = ctx.preflight or {}
        ctx.set_flag("llama_pair", pair)
        ctx.set_flag("cuda", pf.get("cuda"))
        items = self._items(ctx)
        if ctx.dev_mode:
            ctx.ui.say("llama.cpp build: CPU (dev mode)")
            ctx.ui.say("Dev mode: not downloaded: the CUDA runtime, the voice engine and Nightshift Code.")
        else:
            ctx.ui.say(f"llama.cpp build: {pair or 'CPU'} (driver CUDA {pf.get('cuda') or 'n/a'})")
        ctx.download(items)
        self._qwentts(ctx)

    def _qwentts(self, ctx: StepContext) -> None:
        if ctx.dev_mode:
            # no voice engine program and no PyTorch fallback: the voice service is the fake engine
            ctx.set_flag("qwentts", "none")
            ctx.set_flag("tts_fallback", False)
            ctx.ui.say("Dev mode: the voice engine (qwentts.cpp) is not downloaded; the fake engine speaks.")
            return
        item = qwentts_item(ctx.manifest, ctx.paths.home)
        if item is None:
            for url in (QWENTTS_RELEASE_URL, QWENTTS_RELEASE_URL_OLD):
                probe = release_zip_item("qwentts", url, None, ctx.paths.home)
                if probe_release(ctx, probe.url):
                    item = probe
                    break
        if item is not None:
            ctx.download([item])
            ctx.set_flag("qwentts", "binary")
            ctx.set_flag("tts_fallback", False)
            return
        ctx.set_flag("qwentts", "none")
        ctx.set_flag("tts_fallback", True)
        update_runtime(ctx, lambda rt: setattr(rt.tts, "engine", "reference"))
        ctx.warn("NS-W301", "no qwentts.cpp Windows binary is published yet; using the PyTorch engine")

    def verify(self, ctx: StepContext) -> None:
        """Every item's files are in place and the qwentts decision is recorded."""
        for item in self._items(ctx):
            if not is_done(item):
                gone = removed_after_unpack(item)
                if gone:        # unpacked once (the flag says so) and a program is missing now: a scanner
                    assert item.extract_to is not None
                    what = f"was removed after {item.name} was unpacked"
                    raise blocked_error(item.extract_to / gone[0], what)
                raise make_error("NS-E109", f"{item.name} is not unpacked")
        if ctx.flag("qwentts") not in ("binary", "none"):
            raise make_error("NS-E109", "the qwentts.cpp decision was not recorded")
