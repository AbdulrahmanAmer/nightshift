# ruff: noqa: E501
"""include_voicedesign: a designed voice pulls the VoiceDesign talker in setup; an explicit false warns (NS-W114)."""

import json
from pathlib import Path

from installer.stage2.steps.s01_questions import S01Questions
from installer.stage2.steps.s03_download_models import S03DownloadModels
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.web import answers_view

ROOT = Path(__file__).resolve().parents[1]
VOICEDESIGN = "qwen-talker-1.7b-voicedesign-Q8_0.gguf"
DESIGNED = {"mode": "design", "description": "a warm, slow baritone", "name": "narrator", "speaker": None}


def answers(tmp_path: Path, **over):
    return example_answers(home_path=str(tmp_path / "home"), **over)


def names(ctx) -> list[str]:
    return [i.name for i in S03DownloadModels()._items(ctx)]


def test_a_designed_voice_without_the_flag_fetches_the_voicedesign_talker_in_simulate_mode(tmp_path: Path):
    ctx = make_test_context(tmp_path, raw_answers=answers(tmp_path, voice=DESIGNED), ui=FakeUi(interactive=False))
    S01Questions().run(ctx)
    assert ctx.answers is not None and ctx.answers.include_voicedesign is None   # absent: inferred, not rewritten
    assert ctx.warnings == []
    summary = dict(ctx.ui.tables[-1][1])
    assert summary["Voice design model"].startswith("yes (+") and "GB" in summary["Voice design model"]
    step = S03DownloadModels()
    assert VOICEDESIGN in names(ctx) and step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    assert (ctx.paths.models_tts / VOICEDESIGN).is_file()
    saved = json.loads((ctx.paths.config / "answers.json").read_text(encoding="utf-8"))
    assert saved["include_voicedesign"] is None


def test_an_explicit_false_with_a_designed_voice_warns_and_downloads_nothing_extra(tmp_path: Path):
    raw = answers(tmp_path, voice=DESIGNED, include_voicedesign=False)
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=FakeUi(interactive=False))
    S01Questions().run(ctx)
    assert [c for c, _ in ctx.warnings] == ["NS-W114"]
    assert dict(ctx.ui.tables[-1][1])["Voice design model"].startswith("later")
    step = S03DownloadModels()
    assert VOICEDESIGN not in names(ctx)
    step.run(ctx)
    step.verify(ctx)
    assert not (ctx.paths.models_tts / VOICEDESIGN).exists()


def test_preset_voices_keep_the_old_behaviour(tmp_path: Path):
    plain = make_test_context(tmp_path / "a", raw_answers=answers(tmp_path / "a"), ui=FakeUi(interactive=False))
    S01Questions().run(plain)
    assert VOICEDESIGN not in names(plain) and plain.warnings == []
    asked = make_test_context(
        tmp_path / "b", raw_answers=answers(tmp_path / "b", include_voicedesign=True), ui=FakeUi(interactive=False)
    )
    S01Questions().run(asked)
    assert VOICEDESIGN in names(asked) and asked.warnings == []


def test_the_checkbox_defaults_on_for_a_designed_voice(tmp_path: Path):
    design = {"voice": DESIGNED}
    assert answers_view.answers_to_view(design)["includeVoicedesign"] is True
    assert answers_view.answers_to_view({**design, "include_voicedesign": False})["includeVoicedesign"] is False
    assert answers_view.answers_to_view({"voice": {"mode": "preset"}})["includeVoicedesign"] is False
    assert answers_view.answers_to_view({"voice": {"mode": "preset"}, "include_voicedesign": True})[
        "includeVoicedesign"
    ] is True


def test_ns_w114_is_a_known_code_in_the_contract_the_operator_copy_and_the_ui():
    codes = json.loads((ROOT / "contracts" / "errors.json").read_text(encoding="utf-8"))
    assert codes["NS-W114"]["message"] and "include_voicedesign" in codes["NS-W114"]["hint"]
    copy = json.loads((ROOT / "installer" / "stage2" / "web" / "operator_copy.json").read_text(encoding="utf-8"))
    assert copy["NS-W114"]["title"] and copy["NS-W114"]["whatToDo"]
    assert "operator_copy.json" in (ROOT / "installer" / "ui" / "src" / "copy.ts").read_text(encoding="utf-8")
