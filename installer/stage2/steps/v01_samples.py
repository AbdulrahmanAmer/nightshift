"""v01_samples: a spoken sample and a short test render for the setup window's Verify screen (component C1).

Optional, and it never blocks: when either part fails the step ends with the warning ``NS-W403`` and setup
continues. The voice part runs ``studio voice preview``; the render part runs the same fake-services render the
slice gates use (``studio run --fake-services --format shorts_only --count 1``) in a scratch home under
``cache/setup-test``, so the real outbox and database stay clean (docs/design/installer-ui.md 2.6).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from installer.stage2.answers import slugify
from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error
from installer.stage2.web.samples import (
    duration_argv,
    frame_grab_argv,
    newest_file,
    seed_scratch_home,
    wav_seconds,
    write_sine_wav,
)
from installer.stage2.winenv import HOME_VAR

SAMPLE_DIR = ("cache", "setup-test")


def sample_dir(ctx: StepContext) -> Path:
    """Where the samples are written (and served from)."""
    return ctx.paths.home.joinpath(*SAMPLE_DIR)


class V01Samples(Step):
    """Make the voice sample and the test render."""

    id = "v01_samples"
    title = "Test samples"
    optional = True

    def check(self, ctx: StepContext) -> bool:
        """True once the step ran (a failed attempt is not repeated on resume)."""
        return ctx.flag("samples") in ("done", "failed")

    def run(self, ctx: StepContext) -> None:
        """Voice first, then the render; raise NS-W403 at the end if either failed."""
        out = sample_dir(ctx)
        out.mkdir(parents=True, exist_ok=True)
        problems = [p for p in (self._voice(ctx, out), self._render(ctx, out)) if p]
        ctx.set_flag("samples", "failed" if problems else "done")
        if problems:
            raise make_error("NS-W403", "; ".join(problems))

    def verify(self, ctx: StepContext) -> None:
        """Nothing to prove: the samples are a bonus."""

    # ---- voice ----
    def _voice(self, ctx: StepContext, out: Path) -> str | None:
        ans = ctx.ensure_answers()
        speaker = ans.voice.speaker or ans.voice.name or "Ryan"
        wav = out / "voice-sample.wav"
        wav.unlink(missing_ok=True)
        if ctx.simulate:
            write_sine_wav(wav, 1.5)
        else:
            res = ctx.runner.run(
                ctx.tool_argv("studio", ["voice", "preview", speaker, "--out", str(wav)]),
                cwd=ctx.app_path, env={"PYTHONUTF8": "1", HOME_VAR: str(ctx.paths.home)}, timeout=600,
                tag="studio voice preview", meta={"speaker": speaker, "out": str(wav)},
            )
            if not res.ok or not wav.is_file():
                ctx.log.warn("sample_voice_failed", res.text[-200:])
                return f"voice sample: {res.text[-160:] or 'no file written'}"
        ctx.ui.sample("voice", wav, wav_seconds(wav))
        return None

    # ---- render ----
    def _render(self, ctx: StepContext, out: Path) -> str | None:
        if ctx.simulate:
            return None                     # no encoder in CI; the window shows its "not made" note
        ans = ctx.ensure_answers()
        scratch = out / "home"
        shutil.rmtree(scratch, ignore_errors=True)
        seed_scratch_home(ctx.paths.home, scratch, slugify(ans.channel.display_name))
        res = ctx.runner.run(
            ctx.tool_argv("studio", ["run", "--fake-services", "--format", "shorts_only", "--count", "1"]),
            cwd=ctx.app_path, env={"PYTHONUTF8": "1", HOME_VAR: str(scratch)}, timeout=900,
            tag="studio run test render", meta={"home": str(scratch)},
        )
        made = newest_file(scratch / "outbox", "*.mp4") if res.ok else None
        if made is None:
            ctx.log.warn("sample_render_failed", res.text[-200:])
            return f"test render: {res.text[-160:] or 'no video written'}"
        video = out / "render-test.mp4"
        thumb = out / "render-test.jpg"
        shutil.copyfile(made, video)
        grab = ctx.runner.run(frame_grab_argv("ffmpeg", video, thumb), timeout=120, tag="ffmpeg test frame")
        if not grab.ok or not thumb.is_file():
            ctx.log.warn("sample_frame_failed", grab.text[-200:])
            return "test render: could not take a frame"
        ctx.ui.sample("render", video, self._duration(ctx, video))
        ctx.ui.sample("thumb", thumb)
        shutil.rmtree(scratch, ignore_errors=True)
        return None

    @staticmethod
    def _duration(ctx: StepContext, video: Path) -> float | None:
        res = ctx.runner.run(duration_argv("ffprobe", video), timeout=60, tag="ffprobe test render")
        try:
            return round(float(res.stdout.strip().splitlines()[-1]), 2) if res.ok else None
        except (ValueError, IndexError):
            return None
