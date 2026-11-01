"""s02_download_bins: llama.cpp (+ cudart) and OpenCode; qwentts.cpp when a binary exists (component C1).

The llama.cpp pair follows the driver: CUDA 13.0 or newer takes the CUDA 13.4 build (Blackwell needs it),
CUDA 12.4 to 12.x the CUDA 12.4 build, anything older is NS-E103. ``bins.qwentts`` is null in the manifest,
so the GitHub Releases asset is probed; without a binary the PyTorch fallback is scheduled (NS-W301).
"""

from __future__ import annotations

import httpx

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.downloader import DownloadItem, is_done, plan_bins, qwentts_item, release_zip_item
from installer.stage2.errors import make_error
from installer.stage2.runtime_cfg import update_runtime
from installer.stage2.sysinfo import llama_pair_for

QWENTTS_RELEASE_URL = (
    "https://github.com/AbdulrahmanAmer/nightshift/releases/latest/download/qwentts-win-cuda-x64.zip"
)


def choose_pair(ctx: StepContext) -> str | None:
    """cuda13 / cuda12 for the GPU profile (NS-E103 when the driver is too old); None for cpu-dev."""
    if ctx.profile == "cpu-dev":
        return None
    pf = ctx.preflight or {}
    cuda = str(pf.get("cuda") or "")
    pair = llama_pair_for(cuda)
    if pair is None:
        raise make_error("NS-E103", f"nvidia-smi reports CUDA {cuda or 'unknown'}; 12.4 or newer is required")
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
        return plan_bins(
            ctx.manifest, ctx.profile, ctx.paths.home, include_code=answers.install_code,
            llama_pair=choose_pair(ctx),
        )

    def check(self, ctx: StepContext) -> bool:
        """All zips extracted and the qwentts decision recorded."""
        return all(is_done(i) for i in self._items(ctx)) and ctx.flag("qwentts") is not None

    def run(self, ctx: StepContext) -> None:
        """Download the llama.cpp pair (and OpenCode), then settle the qwentts.cpp question."""
        pair = choose_pair(ctx)
        pf = ctx.preflight or {}
        ctx.set_flag("llama_pair", pair)
        ctx.set_flag("cuda", pf.get("cuda"))
        items = self._items(ctx)
        ctx.ui.say(f"llama.cpp build: {pair or 'CPU'} (driver CUDA {pf.get('cuda') or 'n/a'})")
        ctx.download(items)
        self._qwentts(ctx)

    def _qwentts(self, ctx: StepContext) -> None:
        item = qwentts_item(ctx.manifest, ctx.paths.home)
        if item is None:
            probe = release_zip_item("qwentts", QWENTTS_RELEASE_URL, None, ctx.paths.home)
            item = probe if probe_release(ctx, probe.url) else None
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
                raise make_error("NS-E109", f"{item.name} is not unpacked")
        if ctx.flag("qwentts") not in ("binary", "none"):
            raise make_error("NS-E109", "the qwentts.cpp decision was not recorded")
