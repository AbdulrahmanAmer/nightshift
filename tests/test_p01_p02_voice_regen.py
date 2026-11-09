"""P01 x P02: the closing line (`p<n>_close`) is voiced again on a regeneration.

P01 keys the voice signature on `JobContext.generation` so a regeneration never reuses an earlier take; P02
adds the closing-line item, the per-attempt TTS seed and the measured pace. They must compose: every item
of a regenerated pass, the closing line included, is spoken again with the seed of the attempt, and the
hooks stage's restart drops `closings.json` together with `hooks_all.json`.

No FFmpeg: `FakeTts` and the copy-only loudness step of `test_voice_wpm`.
"""

from pathlib import Path

import pytest

from ns_studio.media.voice import narration_items, read_closings, synthesize, tts_seed
from ns_studio.policy import regen
from ns_studio.voice_wpm import load_voice_wpm
from tests.helpers import make_ctx
from tests.test_voice_wpm import TEXT, _no_ffmpeg, _spy_seeds

CLOSING = "Follow for the next story."
HOOK = "Nobody told her the house had a second door."


def _items(ctx):
    parts = [{"n": 1, "text": TEXT, "hook": HOOK}]
    return narration_items(parts, "Ryan", 1.0, None, read_closings(ctx))


def test_the_closing_line_is_spoken_again_on_a_regeneration(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    _no_ffmpeg(monkeypatch)
    ctx = make_ctx(tmp_home)
    ctx.write_json("closings.json", {"1": CLOSING})
    seeds = _spy_seeds(ctx)
    items = _items(ctx)
    assert [it.id for it in items] == ["p1", "p1_hook", "p1_close"]

    manifest, first = synthesize(ctx, items)
    ctx.write_json("audio/manifest.json", manifest)  # what the voice stage does after `synthesize`
    assert first == 3 and seeds == [None]
    _, again = synthesize(ctx, items)  # a resume: same generation, nothing is spoken
    assert again == 0 and seeds == [None]

    ctx.write_json("policy_attempts.json", {"regenerations": 1})  # the policy gate sent the job back
    assert ctx.generation == 1
    manifest, regenerated = synthesize(ctx, _items(ctx))  # even with the same closing text
    assert regenerated == 3 and set(manifest) == {"p1", "p1_hook", "p1_close"}
    assert seeds == [None, tts_seed(ctx)] and seeds[1] is not None
    assert load_voice_wpm(ctx.paths)["voices"]["Ryan"]["jobs"] == 1  # the same job: its sample is replaced


def test_a_regeneration_from_the_hooks_stage_drops_both_memos(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("closings.json", {"1": CLOSING})
    ctx.write_json("hooks_all.json", [["a hook"]])
    ctx.write_json("audio/manifest.json", {})
    regen.invalidate_from(ctx, ["hooks", "seo", "voice"], "hooks")
    assert not ctx.file("closings.json").exists() and not ctx.file("hooks_all.json").exists()
    assert not ctx.file("audio/manifest.json").exists()
    assert read_closings(ctx) is None  # the voice stage then waits for the hooks stage's new line
    assert regen.STAGE_MEMOS["hooks"] == ("hooks_all.json", "closings.json")
