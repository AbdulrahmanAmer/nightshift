"""C1: a reinstall over kept data, and the answers that come with it (defect D-14).

"Uninstall, keep my data" leaves ``config`` (answers, runtime.json, the channel yaml and its voice) and
``models``. A new answers file used to be ignored by s01 (it trusted the saved copy); the first fix made s01
run again, but the tier it saved was only half applied: s03 downloaded the new model while runtime.json kept
the old ``model_path`` and its tuned flags (review of PR 130). The rule, tested here to its effect:

* the new answers win for every choice they name, a choice they leave out stays as it was;
* a changed tier or custom model rebuilds the llama part of runtime.json and forgets the tuning, and the
  install ends on the model file the new answers name;
* the channel yaml and the voice are replaced only where the new answers differ from the kept ones, field by
  field, so the owner's dashboard edits stand; setup says what it kept and how to change it.

A reinstall is a real ``main()`` run of the wizard on the fakes of ``--simulate-downloads`` (the downloader
writes the files it was asked for), over a home that the real ``uninstall.ps1`` kept the data of. The window
writes its choices over ``config/answers.json``; that path is tested with the real controller.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from installer.stage2.reinstall import changes, kept_answers
from installer.stage2.runtime_cfg import ensure_baseline
from installer.stage2.state import load_flags
from installer.stage2.steps.s01_questions import S01Questions
from installer.stage2.steps.s07_configs import channel_from_answers
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.web import answers_view
from installer.stage2.wizard import main
from ns_common.config import load_channel, load_manifest, load_runtime, save_channel, save_runtime
from ns_common.paths import Paths
from tests.test_install_done_means_done import ticking_clock, uninstall_with_the_script  # noqa: F401
from tests.test_wizard_e2e import ROOT, args, started_steps, write_answers

BEST = load_manifest().profiles["gpu"].llm_best
BALANCED = load_manifest().profiles["gpu"].llm_balanced
assert BEST is not None and BALANCED is not None


pytestmark = pytest.mark.usefixtures("ticking_clock")  # two installs in one test get two ids


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)


def edit_answers(answers: Path, **over) -> None:
    data = json.loads(answers.read_text(encoding="utf-8"))
    data.update(over)
    answers.write_text(json.dumps(data), encoding="utf-8")


def owner_edits_the_channel(home: Path, slug: str = "after-dark-tales") -> None:
    """What the dashboard and a hand edit change after setup: the channel's own settings, a port."""
    p = Paths(home)
    ch = load_channel(p, slug)
    ch.niche, ch.critic_bar = "cosmic horror", 9.5
    save_channel(p, ch)
    rt = load_runtime(p)
    rt.llama.port = 8085
    save_runtime(p, rt)


def a_home_that_kept_its_data(tmp_path: Path, **first) -> tuple[Path, Path]:
    """Install on the Balanced model, tune, let the owner edit, uninstall with the real script (keep data)."""
    answers = write_answers(tmp_path, llm_tier="balanced", **first)
    home = tmp_path / "home"
    assert main(args(tmp_path, answers)) == 0
    rt = load_runtime(Paths(home))
    assert rt.llama.model_path.endswith(BALANCED.file) and rt.llama.flags.n_cpu_moe == 8
    assert load_flags(Paths(home))["tuned"] in ("tuned", "defaults")
    owner_edits_the_channel(home)
    uninstall_with_the_script(home)
    assert (home / "config" / "runtime.json").is_file() and not (
        home / "config" / "wizard_flags.json"
    ).exists()
    return home, answers


# ---------------------------------------------------------------------------------------- the console flow
def test_a_new_answers_file_wins_after_an_uninstall_that_keeps_data(tmp_path: Path) -> None:
    home, answers = a_home_that_kept_its_data(tmp_path)
    edit_answers(
        answers,
        llm_tier="best",
        run_time="04:15",
        voice={"mode": "design", "description": "a calm night-shift narrator", "name": "QaNarrator"},
    )
    assert main(args(tmp_path, answers)) == 0

    p = Paths(home)
    rt = load_runtime(p)
    # the install ends on the model the new answers name: runtime.json points at it and the file is there
    assert rt.llama.model_path.endswith(BEST.file) and Path(rt.llama.model_path).is_file()
    assert rt.llama.flags.n_cpu_moe == 12 and rt.llama.flags.ctx == 32768  # the Best model's settings
    assert rt.llama.port == 8085  # the owner's port stays
    assert Path(
        rt.llama.model_path.replace(BEST.file, BALANCED.file)
    ).is_file()  # the old file is not deleted
    saved = json.loads((home / "config" / "answers.json").read_text(encoding="utf-8"))
    assert (
        saved["llm_tier"] == "best"
        and saved["voice"]["name"] == "QaNarrator"
        and saved["run_time"] == "04:15"
    )
    # the channel: the voice is the new one, the owner's edits stand
    ch = load_channel(p, "after-dark-tales")
    assert ch.voice.mode == "design" and ch.voice.name == "QaNarrator"
    assert ch.niche == "cosmic horror" and ch.critic_bar == 9.5
    pending = json.loads((home / "config" / "pending_voice.json").read_text(encoding="utf-8"))
    assert pending["name"] == "QaNarrator" and pending["mode"] == "design"
    # the new model was tuned (not the old one's flags kept) and the nightly task has the new time
    assert started_steps(home).count("s06_tune") == 2
    assert load_flags(p)["tuned"] in ("tuned", "defaults") and load_flags(p)["schedule_time"] == "04:15"


def test_the_same_answers_after_an_uninstall_keep_the_runtime_and_the_channel(tmp_path: Path) -> None:
    home, answers = a_home_that_kept_its_data(tmp_path)
    before = load_runtime(Paths(home))
    assert main(args(tmp_path, answers)) == 0
    after = load_runtime(Paths(home))
    assert after.llama.model_path == before.llama.model_path and after.llama.port == 8085
    assert after.llama.flags.n_cpu_moe == 8
    assert load_channel(Paths(home), "after-dark-tales").niche == "cosmic horror"
    assert not (home / "config" / "pending_voice.json").exists()


def test_a_finished_install_run_again_with_a_changed_tier_tunes_the_new_model(tmp_path: Path) -> None:
    """No uninstall in between: the flags of the finished install are still there and must not be trusted."""
    answers = write_answers(tmp_path, llm_tier="balanced", run_time="03:00")
    home = tmp_path / "home"
    assert main(args(tmp_path, answers)) == 0
    assert started_steps(home).count("s06_tune") == 1
    assert main(args(tmp_path, answers)) == 0
    assert started_steps(home).count("s01_questions") == 1  # nothing changed: nothing restarts
    edit_answers(answers, llm_tier="best", run_time="05:30")
    assert main(args(tmp_path, answers)) == 0
    steps = started_steps(home)
    assert (
        steps.count("s01_questions") == 2
        and steps.count("s06_tune") == 2
        and steps.count("s09_shortcuts_scheduler") == 2
    )
    rt = load_runtime(Paths(home))
    assert rt.llama.model_path.endswith(BEST.file) and Path(rt.llama.model_path).is_file()
    assert rt.llama.flags.n_cpu_moe == 12
    assert load_flags(Paths(home))["schedule_time"] == "05:30"


# ---------------------------------------------------------------------------------------- the window flow
def test_the_window_keeps_the_earlier_answers_so_the_wizard_can_apply_the_new_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window writes its choices over config/answers.json and starts the wizard with --answers on that
    file, so the wizard cannot see a changed source. The controller keeps the old file first."""
    from tests.test_installer_web_controller import Setup, finish_script

    home, answers = a_home_that_kept_its_data(tmp_path)
    old = json.loads((home / "config" / "answers.json").read_text(encoding="utf-8"))
    s = Setup(tmp_path, monkeypatch, script=finish_script)
    s.accept(llmTier="best", homePath=str(home), voice={"speaker": "Aiden"})
    assert s.ctl.do_action("start")[0] == 200
    s.until(lambda st: st["run"] == "finished", what="the scripted run")
    kept = home / "config" / answers_view.KEPT_NAME
    assert json.loads(kept.read_text(encoding="utf-8")) == old  # the earlier answers, untouched
    now = json.loads((home / "config" / "answers.json").read_text(encoding="utf-8"))
    assert now["llm_tier"] == "best" and now["voice"]["speaker"] == "Aiden"

    # the wizard the window starts: --answers is config/answers.json itself
    window_args = [
        "--simulate-downloads",
        "--non-interactive",
        "--no-head-checks",
        "--home",
        str(home),
        "--answers",
        str(home / "config" / "answers.json"),
        "--app-path",
        str(ROOT),
        "--preflight",
        str(tmp_path / "no-preflight.json"),
    ]
    assert main(window_args) == 0
    rt = load_runtime(Paths(home))
    assert rt.llama.model_path.endswith(BEST.file) and Path(rt.llama.model_path).is_file()
    assert rt.llama.flags.n_cpu_moe == 12
    ch = load_channel(Paths(home), "after-dark-tales")
    assert ch.voice.speaker == "Aiden" and ch.niche == "cosmic horror"  # the voice changed, the edit stands
    assert not kept.exists()  # used up


def test_the_earliest_copy_is_the_one_kept(tmp_path: Path) -> None:
    target = tmp_path / "config" / "answers.json"
    target.parent.mkdir()
    answers_view.keep_earlier_answers(target)  # nothing to keep yet
    assert not (target.parent / answers_view.KEPT_NAME).exists()
    target.write_text('{"llm_tier": "balanced"}', encoding="utf-8")
    answers_view.keep_earlier_answers(target)
    target.write_text('{"llm_tier": "best"}', encoding="utf-8")  # a first attempt that stopped
    answers_view.keep_earlier_answers(target)
    kept = (target.parent / answers_view.KEPT_NAME).read_text(encoding="utf-8")
    assert json.loads(kept) == {"llm_tier": "balanced"}


# ---------------------------------------------------------------------------------------- step level
def install_with(tmp_path: Path, **answers):
    """A home as setup leaves it: saved answers, runtime.json and the channel yaml (s01, s06, s07 short)."""
    ctx = make_test_context(tmp_path, raw_answers=example_answers(**answers), ui=FakeUi(interactive=False))
    S01Questions().run(ctx)
    ensure_baseline(ctx)
    assert ctx.answers is not None
    save_channel(ctx.paths, channel_from_answers(ctx.answers))
    return ctx


def reinstall_ctx(tmp_path: Path, raw: dict, name: str = "new-answers.json", **kw):
    """A context whose answers come from a file outside config (next to INSTALL.cmd or --answers)."""
    src = tmp_path / name
    src.write_text(json.dumps(raw), encoding="utf-8")
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=FakeUi(interactive=False), **kw)
    ctx.answers_path = src
    return ctx


def test_what_the_file_leaves_out_is_not_a_change_and_stays_as_it_was(tmp_path: Path) -> None:
    install_with(tmp_path, llm_tier="balanced", run_time="05:30", masters_per_week=2)
    ctx = reinstall_ctx(tmp_path, {"run_time": "06:00"})  # names one choice only
    earlier = kept_answers(ctx)
    assert earlier is not None and [c.key for c in changes(ctx, earlier[0], ctx.raw_answers)] == ["run_time"]
    step = S01Questions()
    assert step.check(ctx) is False
    step.run(ctx)
    saved = json.loads((ctx.paths.config / "answers.json").read_text(encoding="utf-8"))
    assert saved["run_time"] == "06:00"
    assert saved["llm_tier"] == "balanced" and saved["masters_per_week"] == 2  # the silence kept them
    assert saved["channel"]["display_name"] == "After Dark Tales"
    same = reinstall_ctx(tmp_path, {"run_time": "06:00"})
    assert S01Questions().check(same) is True and same.answers is not None


def test_spelling_differences_are_not_changes(tmp_path: Path) -> None:
    install_with(tmp_path, llm_tier="balanced")
    wordy = example_answers(
        llm_tier="balanced",
        voice={"mode": "preset", "speaker": "Ryan", "speed": 1.0, "name": None},
        channel={
            "display_name": "After Dark Tales",
            "niche": "horror fiction",
        },  # language and seeds by default
        include_voicedesign=False,
        llm_custom=None,
        pexels_api_key="new-key",
    )
    wordy["channel"]["language"], wordy["channel"]["seed_keywords"] = "en", ["haunted house"]
    ctx = reinstall_ctx(tmp_path, wordy)
    assert S01Questions().check(ctx) is True  # a new key is not a choice either


def test_a_dev_install_does_not_see_the_limits_it_applies_as_changes(tmp_path: Path) -> None:
    ctx = make_test_context(
        tmp_path,
        raw_answers=example_answers(llm_tier="best", install_code=True),
        ui=FakeUi(interactive=False),
        dev_mode=True,
    )
    S01Questions().run(ctx)
    saved = json.loads((ctx.paths.config / "answers.json").read_text(encoding="utf-8"))
    assert saved["llm_tier"] == "small" and saved["install_code"] is False  # what a dev install uses
    again = reinstall_ctx(tmp_path, example_answers(llm_tier="best", install_code=True), dev_mode=True)
    assert S01Questions().check(again) is True  # still the same request: no endless re-run


def test_a_changed_tier_rebuilds_the_llama_part_and_says_what_it_kept(tmp_path: Path) -> None:
    kept = install_with(tmp_path, llm_tier="balanced")
    rt = load_runtime(kept.paths)
    rt.llama.port, rt.llama.flags.n_gpu_layers = 8085, 33  # tuning found 33 layers
    save_runtime(kept.paths, rt)
    kept.set_flag("tuned", "tuned")
    ctx = reinstall_ctx(tmp_path, example_answers(llm_tier="best"))
    step = S01Questions()
    assert step.check(ctx) is False
    step.run(ctx)
    now = load_runtime(ctx.paths)
    assert now.llama.model_path.endswith(BEST.file) and now.llama.flags.n_gpu_layers == 99  # not the tuned 33
    assert now.llama.flags.n_cpu_moe == 12 and now.llama.flags.kv_type == "q8_0" and now.llama.port == 8085
    assert ctx.flag("tuned") is None  # s06 tunes the new model
    said = ctx.ui.text
    assert "differ from your earlier install's (config\\answers.json): llm_tier: balanced -> best" in said
    assert f"now uses {BEST.file} (it had {BALANCED.file})" in said and "stays in models\\llm" in said
    assert not (ctx.paths.config / answers_view.KEPT_NAME).exists()


def test_a_custom_story_model_replaces_the_tier_model_in_runtime_json(tmp_path: Path) -> None:
    install_with(tmp_path, llm_tier="balanced")
    custom = {"repo": "someone/some-model-GGUF", "file": "some-model-Q4_K_M.gguf", "size": 5_000_000_000}
    ctx = reinstall_ctx(tmp_path, example_answers(llm_tier="balanced", llm_custom=custom))
    S01Questions().run(ctx)
    assert load_runtime(ctx.paths).llama.model_path.endswith("some-model-Q4_K_M.gguf")
    assert load_runtime(ctx.paths).llama.flags.n_cpu_moe == 0  # a custom model: plain flags


def test_the_channel_yaml_is_changed_field_by_field_and_the_owners_edits_stand(tmp_path: Path) -> None:
    kept = install_with(tmp_path, masters_per_week=1)
    ch = load_channel(kept.paths, "after-dark-tales")
    ch.niche, ch.pools.hooks, ch.seed_keywords = "cosmic horror", ["warning"], ["lighthouse"]
    save_channel(kept.paths, ch)
    new = example_answers(masters_per_week=3)
    new["channel"] = {
        "display_name": "After Dark Tales",
        "niche": "horror fiction",
        "language": "de",
        "seed_keywords": ["haunted house"],
    }  # niche and seeds as before
    ctx = reinstall_ctx(tmp_path, new)
    S01Questions().run(ctx)
    now = load_channel(ctx.paths, "after-dark-tales")
    assert now.language == "de" and now.masters_per_week == 3  # the answers changed these
    assert now.niche == "cosmic horror" and now.seed_keywords == ["lighthouse"]  # they did not change these
    assert now.pools.hooks == ["warning"]
    assert "your answers changed language, masters per week" in ctx.ui.text


def test_a_new_channel_name_makes_a_new_channel_and_names_the_old_one_that_stays(tmp_path: Path) -> None:
    kept = install_with(tmp_path)
    new = example_answers()
    new["channel"] = {"display_name": "Daylight Tales", "niche": "cozy stories"}
    ctx = reinstall_ctx(tmp_path, new)
    S01Questions().run(ctx)
    assert (
        "Your answers name a new channel, 'daylight-tales'. Channel 'after-dark-tales' was kept"
        in ctx.ui.text
    )
    assert kept.paths.channel_file("after-dark-tales").is_file()


def test_a_voice_that_exists_is_kept_and_a_preset_forgets_a_designed_voice_nobody_made(
    tmp_path: Path,
) -> None:
    design = {"mode": "design", "description": "calm", "name": "qa-voice"}
    kept = install_with(tmp_path, voice=design)
    (kept.paths.models_tts_voices / "qa-voice").mkdir(parents=True)
    (kept.paths.models_tts_voices / "qa-voice" / "voice.json").write_text("{}", encoding="utf-8")
    ctx = reinstall_ctx(tmp_path, example_answers(voice={**design, "description": "warm and slow"}))
    S01Questions().run(ctx)
    assert "A voice named 'qa-voice' already exists" in ctx.ui.text
    assert not (ctx.paths.config / "pending_voice.json").exists()  # nothing is made over it

    (ctx.paths.config / "pending_voice.json").write_text(
        json.dumps({"channel": "after-dark-tales", "mode": "design", "name": "qa-voice"}), encoding="utf-8"
    )
    preset = reinstall_ctx(tmp_path, example_answers(voice={"mode": "preset", "speaker": "Serena"}), "p.json")
    S01Questions().run(preset)
    assert load_channel(preset.paths, "after-dark-tales").voice.speaker == "Serena"
    assert not (preset.paths.config / "pending_voice.json").exists()  # the request for the old one is gone


def test_a_runtime_json_made_for_another_kind_of_install_is_built_again(tmp_path: Path) -> None:
    kept = install_with(tmp_path, llm_tier="balanced")
    rt = load_runtime(kept.paths)
    rt.dev_mode = True  # left by a --dev install
    save_runtime(kept.paths, rt)
    ctx = reinstall_ctx(tmp_path, example_answers(llm_tier="balanced", masters_per_week=2))
    S01Questions().run(ctx)
    assert load_runtime(ctx.paths).dev_mode is False
    assert "was made for another kind of install" in ctx.ui.text


def test_an_engine_test_answer_that_changes_reaches_runtime_json(tmp_path: Path) -> None:
    install_with(tmp_path)
    ctx = reinstall_ctx(tmp_path, example_answers(fake_voice=True, fake_footage=True))
    S01Questions().run(ctx)
    rt = load_runtime(ctx.paths)
    assert rt.tts.engine == "fake" and rt.fake_footage is True
    back = reinstall_ctx(tmp_path, example_answers(fake_voice=False, fake_footage=False), "back.json")
    S01Questions().run(back)
    assert load_runtime(back.paths).tts.engine == "qwentts" and load_runtime(back.paths).fake_footage is False


def test_the_name_and_theme_in_ui_prefs_follow_only_the_answers_that_changed(tmp_path: Path) -> None:
    kept = install_with(tmp_path, owner_name="Abdo", ui_theme="dark")
    prefs = kept.paths.config / "ui_prefs.json"
    prefs.write_text(
        json.dumps({"owner_name": "Abdo", "theme": "soft"}), encoding="utf-8"
    )  # picked in the dashboard
    ctx = reinstall_ctx(tmp_path, example_answers(owner_name="Abdulrahman", ui_theme="dark"))
    S01Questions().run(ctx)
    assert json.loads(prefs.read_text(encoding="utf-8")) == {"owner_name": "Abdulrahman", "theme": "soft"}


def test_setup_says_which_earlier_files_it_is_using_when_nothing_changed(tmp_path: Path) -> None:
    install_with(tmp_path, llm_tier="balanced")
    same = reinstall_ctx(tmp_path, example_answers(llm_tier="balanced"))
    step = S01Questions()
    assert step.check(same) is True
    step.verify(same)
    text = same.ui.text
    assert "name the same choices as your earlier install; nothing was changed" in text
    assert (
        "Kept config\\runtime.json (ports, tuning, engines)" in text
        and "Kept channel 'after-dark-tales'" in text
    )
    # no new file at all: the saved answers are the ones in use, and the line says how to change them
    alone = make_test_context(tmp_path, ui=FakeUi(interactive=False))
    alone.answers_path = alone.paths.config / "answers.json"
    alone.raw_answers = json.loads(alone.answers_path.read_text(encoding="utf-8"))
    step = S01Questions()
    assert step.check(alone) is True
    step.verify(alone)
    assert (
        "Using the answers from your earlier install (config\\answers.json). To change a choice"
        in alone.ui.text
    )


def test_a_first_install_says_nothing_about_kept_files(tmp_path: Path) -> None:
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), ui=FakeUi(interactive=False))
    step = S01Questions()
    step.run(ctx)
    step.verify(ctx)
    assert "Kept" not in ctx.ui.text and "differ" not in ctx.ui.text


def test_deleting_the_saved_answers_to_start_over_does_not_leave_the_old_model_in_runtime_json(
    tmp_path: Path,
) -> None:
    """Setup says "delete config answers.json to be asked again": the model must follow the new answers."""
    kept = install_with(tmp_path, llm_tier="balanced")
    (kept.paths.config / "answers.json").unlink()  # nothing left to compare with
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(llm_tier="best"), ui=FakeUi(interactive=False)
    )
    S01Questions().run(ctx)
    assert load_runtime(ctx.paths).llama.model_path.endswith(BEST.file)
    assert load_runtime(ctx.paths).llama.flags.n_cpu_moe == 12
    # a model that is not one of setup's own (picked by hand) is left alone
    rt = load_runtime(ctx.paths)
    rt.llama.model_path = str(ctx.paths.models_llm / "my-own-finetune.gguf")
    save_runtime(ctx.paths, rt)
    (ctx.paths.config / "answers.json").unlink()
    again = make_test_context(tmp_path, raw_answers=example_answers(llm_tier="balanced"), ui=FakeUi(False))
    S01Questions().run(again)
    assert load_runtime(again.paths).llama.model_path.endswith("my-own-finetune.gguf")


def test_a_custom_model_with_a_tier_in_the_file_is_not_a_change_every_time(tmp_path: Path) -> None:
    custom = {"repo": "someone/some-model-GGUF", "file": "some-model-Q4_K_M.gguf", "size": 5_000_000_000}
    ctx = make_test_context(tmp_path, raw_answers=example_answers(llm_tier="best", llm_custom=custom))
    S01Questions().run(ctx)
    saved = json.loads((ctx.paths.config / "answers.json").read_text(encoding="utf-8"))
    assert saved["llm_tier"] == "small"  # s01 saves the plain tier with a custom model
    again = reinstall_ctx(tmp_path, example_answers(llm_tier="best", llm_custom=custom))
    assert S01Questions().check(again) is True


def test_the_yaml_the_dashboard_writes_is_what_the_channel_edit_starts_from(tmp_path: Path) -> None:
    """Guard for the field-by-field edit: the kept yaml is read and written whole, not rebuilt."""
    kept = install_with(tmp_path)
    f = kept.paths.channel_file("after-dark-tales")
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    data["creator_note"] = "never mention the lighthouse"
    f.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    ctx = reinstall_ctx(tmp_path, example_answers(voice={"mode": "preset", "speaker": "Vivian"}))
    S01Questions().run(ctx)
    assert load_channel(ctx.paths, "after-dark-tales").creator_note == "never mention the lighthouse"
