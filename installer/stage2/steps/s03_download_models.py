"""s03_download_models: LLM, embeddings, TTS GGUFs, faster-whisper, YuNet (component C1, spec 21.3).

Resumable and verified (see downloader). A custom story model (``llm_custom``, named by the owner,
CR-C1-UI-9) is the one file downloaded without a pinned SHA256: the step says so with NS-W113 and points
``runtime.json`` at it. With ``models_source`` set, files whose name and SHA256 match are
copied from that folder and the rest is downloaded. When the PyTorch TTS fallback is scheduled the original
safetensors are added to the plan.
"""

from __future__ import annotations

from pathlib import Path

from installer.stage2.answers import wants_voicedesign
from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.devmode import remove_stubs
from installer.stage2.downloader import (
    DownloadItem,
    human_bytes,
    is_done,
    pending,
    plan_models,
    total_bytes,
)
from installer.stage2.errors import make_error
from installer.stage2.fallback import fallback_items
from installer.stage2.runtime_cfg import update_runtime


class S03DownloadModels(Step):
    """Download (or copy) every model file."""

    id = "s03_download_models"
    title = "Download AI models"
    clears_on_run = ("smoke", "smoke_version")          # new models: the smoke test tested the old ones

    def _items(self, ctx: StepContext, *, with_fallback: bool = True) -> list[DownloadItem]:
        items = plan_models(
            ctx.manifest, ctx.profile, ctx.tier, ctx.paths.home, custom=ctx.llm_custom,
            include_voicedesign=wants_voicedesign(ctx.ensure_answers()), dev=ctx.dev_mode,
            include_voice=not ctx.fake_voice,
        )
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
        if not ctx.dev_mode:
            for gone in remove_stubs(ctx.paths.home / "models"):      # left by an earlier `--dev` install
                ctx.log.info("dev_stub_removed", file=str(gone))
        items = self._items(ctx)
        todo = self._outstanding(ctx, items)
        size = total_bytes(todo)
        source = answers.models_source
        if ctx.dev_mode:
            ctx.ui.say(
                "Dev mode: no model is downloaded. Small placeholder files with the real names are written "
                "(marked .dev-stub); the face finder is the real, tiny file."
            )
        if source:
            ctx.ui.say(f"Looking for matching files in {source} before downloading.")
        ctx.ui.say(
            f"{len(todo)} files to fetch, {human_bytes(size)} in total "
            "(the speed and the time left are measured once the first file is running)."
        )
        ctx.ui.say("You can close this window at any time and run INSTALL.cmd again to resume.", "cyan")
        custom = ctx.llm_custom
        if custom is not None:
            ctx.warn("NS-W113", f"{custom.repo}/{custom.file}")
        result = ctx.download(todo, models_source=source)
        ctx.set_flag("missing_optional", sorted(result.missing_optional))
        if custom is not None:
            self._point_runtime_at(ctx, custom.file)
        ctx.log.info(
            "models_done", downloaded=len(result.downloaded), copied=len(result.copied),
            skipped=len(result.skipped),
        )
        ctx.ui.say(
            f"Done: {len(result.downloaded)} downloaded, {len(result.copied)} copied from your folder, "
            f"{len(result.skipped)} already present."
        )

    @staticmethod
    def _point_runtime_at(ctx: StepContext, file: str) -> None:
        """An existing runtime.json gets the custom model path; a new one is built with it."""
        if ctx.paths.runtime_file.exists():
            path = str(ctx.paths.models_llm / Path(file).name)
            update_runtime(ctx, lambda rt: setattr(rt.llama, "model_path", path))

    def verify(self, ctx: StepContext) -> None:
        """Every required file exists (the verified marker proves its SHA256)."""
        absent_ok = set(ctx.flag("missing_optional", []))
        for item in self._items(ctx):
            if item.optional and item.name in absent_ok:
                continue
            if not Path(item.dest).is_file() or not is_done(item):
                raise make_error("NS-E109", f"{item.dest.name} is missing or not verified")
