"""s03_download_models: LLM, embeddings, TTS GGUFs, faster-whisper, YuNet (component C1, spec 21.3).

Resumable and verified (see downloader). With ``models_source`` set, files whose name and SHA256 match are
copied from that folder and the rest is downloaded. When the PyTorch TTS fallback is scheduled the original
safetensors are added to the plan.
"""

from __future__ import annotations

from pathlib import Path

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.downloader import (
    DownloadItem,
    eta_text,
    human_bytes,
    is_done,
    pending,
    plan_models,
    total_bytes,
)
from installer.stage2.errors import make_error
from installer.stage2.fallback import fallback_items


class S03DownloadModels(Step):
    """Download (or copy) every model file."""

    id = "s03_download_models"
    title = "Download AI models"

    def _items(self, ctx: StepContext, *, with_fallback: bool = True) -> list[DownloadItem]:
        items = plan_models(ctx.manifest, ctx.profile, ctx.tier, ctx.paths.home)
        if with_fallback and ctx.flag("tts_fallback", False):
            items += fallback_items(ctx)
        return items

    def _outstanding(self, ctx: StepContext, items: list[DownloadItem]) -> list[DownloadItem]:
        absent_ok = set(ctx.flag("missing_optional", []))
        return [i for i in pending(items) if not (i.optional and i.name in absent_ok)]

    def check(self, ctx: StepContext) -> bool:
        """True when every file is already in place and verified."""
        return not self._outstanding(ctx, self._items(ctx))

    def run(self, ctx: StepContext) -> None:
        """Print the plan, then download or copy."""
        answers = ctx.ensure_answers()
        items = self._items(ctx)
        todo = self._outstanding(ctx, items)
        size = total_bytes(todo)
        source = answers.models_source
        if source:
            ctx.ui.say(f"Looking for matching files in {source} before downloading.")
        ctx.ui.say(
            f"{len(todo)} files to fetch, {human_bytes(size)} in total "
            f"(about {eta_text(size)} at 50 Mbit/s; the bars below show the real speed)."
        )
        ctx.ui.say("You can close this window at any time and run INSTALL.cmd again to resume.", "cyan")
        result = ctx.download(todo, models_source=source)
        ctx.set_flag("missing_optional", sorted(result.missing_optional))
        ctx.log.info(
            "models_done", downloaded=len(result.downloaded), copied=len(result.copied),
            skipped=len(result.skipped),
        )
        ctx.ui.say(
            f"Done: {len(result.downloaded)} downloaded, {len(result.copied)} copied from your folder, "
            f"{len(result.skipped)} already present."
        )

    def verify(self, ctx: StepContext) -> None:
        """Every required file exists (the verified marker proves its SHA256)."""
        absent_ok = set(ctx.flag("missing_optional", []))
        for item in self._items(ctx):
            if item.optional and item.name in absent_ok:
                continue
            if not Path(item.dest).is_file() or not is_done(item):
                raise make_error("NS-E109", f"{item.dest.name} is missing or not verified")
