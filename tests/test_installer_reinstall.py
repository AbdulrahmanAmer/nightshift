"""C1: a reinstall over kept data, and the answers that come with it (defect D-14).

"Uninstall, keep my data" leaves ``config`` (answers, runtime.json, the channel yaml and its voice) and
``models``. A new answers file used to be ignored by s01 (it trusted the saved copy); the first fix made s01
run again, but the tier it saved was only half applied: s03 downloaded the new model while runtime.json kept
the old ``model_path`` and its tuned flags (review of PR 130). The rule, tested here to its effect:

* the new answers win for every choice they name, a choice they leave out stays as it was;
* a changed tier or custom model rebuilds the llama part of runtime.json and forgets the tuning, and the
  install ends on the model file the new answers name;
* the channel yaml and the voice are replaced only where the new answers differ from the kept ones, field by
  field, so the owner's dashboard edits stand; setup says what it kept and how to change it;
* runtime.json is pointed at a new model only once that model is downloaded and checked, so the nightly run
  keeps working on the old one until then (review round 1 of PR 136).

A reinstall is a real ``main()`` run of the wizard on the fakes of ``--simulate-downloads`` (the downloader
writes the files it was asked for), over a home that the real ``uninstall.ps1`` kept the data of. The window
writes its choices over ``config/answers.json``; that path is tested with the real controller. ``probe``
watches the simulated PC while setup runs: which model runtime.json named, and whether the file was there,
at every command and every download.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from installer.stage2 import reinstall, simulate, wizard
from installer.stage2.reinstall import changes, kept_answers
from installer.stage2.runtime_cfg import ensure_baseline
from installer.stage2.secrets import MemoryStore
from installer.stage2.state import load_flags
from installer.stage2.steps.s01_questions import S01Questions
from installer.stage2.steps.s06_tune import S06Tune
from installer.stage2.steps.s07_configs import channel_from_answers
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.web import answers_view
from installer.stage2.wizard import main
from ns_common.config import load_channel, load_manifest, load_runtime, save_channel, save_runtime
from ns_common.paths import Paths
from tests.test_install_done_means_done import Killed, ticking_clock, uninstall_with_the_script  # noqa: F401
from tests.test_wizard_e2e import ROOT, args, started_steps, write_answers

BEST = load_manifest().profiles["gpu"].llm_best
BALANCED = load_manifest().profiles["gpu"].llm_balanced
assert BEST is not None and BALANCED is not None
CUSTOM = {"repo": "someone/some-model-GGUF", "file": "some-model-Q4_K_M.gguf", "size": 5_000_000_000}


pytestmark = pytest.mark.usefixtures("ticking_clock")  # two installs in one test get two ids


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)


class Probe:
    """What the simulated PC saw while setup ran, as the nightly task would have seen it."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.verify_needs_the_voice_program = False
        self.downloads: list[tuple[str, str]] = []  # (file being fetched, the model runtime.json names then)
        self.calls: list[tuple[str, str]] = []  # (command, the model runtime.json names then)

    def reset(self) -> None:
        self.downloads, self.calls = [], []

    def runtime(self) -> dict:
        try:
            return json.loads((self.home / "config" / "runtime.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def model(self) -> str:
        """The model file runtime.json names right now, or what is wrong with it (missing from disk)."""
        path = (self.runtime().get("llama") or {}).get("model_path")
        if not path:
            return "(no runtime.json)"
        return Path(path).name if Path(path).is_file() else f"MISSING {Path(path).name}"

    @property
    def tunes(self) -> list[str]:
        return [model for tag, model in self.calls if tag == "ns tune"]

    @property
    def schedules(self) -> list[str]:
        return [tag for tag, _ in self.calls if tag.startswith("ns schedule install")]

    def before_the_first_tune(self) -> list[str]:
        """The model at every command and every download before tuning began."""
        seen = [model for _, model in self.downloads]
        for tag, model in self.calls:
            if tag == "ns tune":
                break
            seen.append(model)
        return seen


@pytest.fixture
def probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Probe:
    watch = Probe(tmp_path / "home")

    class WatchedRunner(simulate.FakeRunner):
        def run(self, argv, **kw):  # type: ignore[no-untyped-def]
            tag = kw.get("tag", "")
            watch.calls.append((tag, watch.model()))
            if tag == "ns verify --quick" and watch.verify_needs_the_voice_program:
                engine = (watch.runtime().get("tts") or {}).get("engine")
                program = watch.home / "bin" / "qwentts" / "qwen-tts.exe"
                # the real `ns verify` fails the voice checks when the engine named is not on disk
                self.verify_failures = (
                    {"tts.voices"} if engine == "qwentts" and not program.is_file() else set()
                )
            return super().run(argv, **kw)

    real_fetch = simulate.simulated_fetch

    def watched_fetch(items, **kw):  # type: ignore[no-untyped-def]
        fetch = real_fetch(items, **kw)

        def inner(url, dest, *a, **k):  # type: ignore[no-untyped-def]
            watch.downloads.append((str(dest), watch.model()))
            return fetch(url, dest, *a, **k)

        return inner

    monkeypatch.setattr(wizard, "FakeRunner", WatchedRunner)
    monkeypatch.setattr(simulate, "simulated_fetch", watched_fetch)
    return watch


def edit_answers(answers: Path, **over) -> None:
    data = json.loads(answers.read_text(encoding="utf-8"))
    data.update(over)
    answers.write_text(json.dumps(data), encoding="utf-8")


def owner_edits_the_channel(home: Path, slug: str = "after-dark-tales") -> None:
    """What the dashboard and a hand edit change after setup: the channel's own settings, a port."""
    p = Paths(home)
    ch = load_channel(p, slug)
    ch.niche, ch.critic_bar, ch.seed_keywords = "cosmic horror", 9.5, ["lighthouse"]
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
def test_a_new_answers_file_wins_after_an_uninstall_that_keeps_data(tmp_path: Path, probe: Probe) -> None:
    home, answers = a_home_that_kept_its_data(tmp_path)
    edit_answers(
        answers,
        llm_tier="best",
        run_time="04:15",
        voice={"mode": "design", "description": "a calm night-shift narrator", "name": "QaNarrator"},
    )
    probe.reset()
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
    assert ch.niche == "cosmic horror" and ch.critic_bar == 9.5 and ch.seed_keywords == ["lighthouse"]
    pending = json.loads((home / "config" / "pending_voice.json").read_text(encoding="utf-8"))
    assert pending["name"] == "QaNarrator" and pending["mode"] == "design"
    # the new model was tuned (the sweep ran with runtime.json on the Best file, not the old model's flags)
    assert probe.tunes == [BEST.file] and load_flags(p)["tuned"] in ("tuned", "defaults")
    # and the nightly task was registered again for the new time
    assert [t for t in probe.schedules if "--time 04:15" in t] and not [
        t for t in probe.schedules if "--time 03:00" in t
    ]


def test_the_same_answers_after_an_uninstall_keep_the_runtime_and_the_channel(tmp_path: Path) -> None:
    home, answers = a_home_that_kept_its_data(tmp_path)
    before = load_runtime(Paths(home))
    assert main(args(tmp_path, answers)) == 0
    after = load_runtime(Paths(home))
    assert after.llama.model_path == before.llama.model_path and after.llama.port == 8085
    assert after.llama.flags.n_cpu_moe == 8
    assert load_channel(Paths(home), "after-dark-tales").niche == "cosmic horror"
    assert not (home / "config" / "pending_voice.json").exists()


def test_a_finished_install_run_again_with_a_changed_tier_tunes_the_new_model(
    tmp_path: Path, probe: Probe
) -> None:
    """No uninstall in between: the flags of the finished install are still there and must not be trusted."""
    answers = write_answers(tmp_path, llm_tier="balanced", run_time="03:00")
    home = tmp_path / "home"
    assert main(args(tmp_path, answers)) == 0
    assert probe.tunes == [BALANCED.file] and len(probe.schedules) == 1
    assert main(args(tmp_path, answers)) == 0
    assert started_steps(home).count("s01_questions") == 1  # nothing changed: nothing restarts
    assert len(probe.tunes) == 1 and len(probe.schedules) == 1
    edit_answers(answers, llm_tier="best", run_time="05:30")
    assert main(args(tmp_path, answers)) == 0
    # tuning ran again, and on the new model; the nightly task was registered again for the new time
    assert probe.tunes == [BALANCED.file, BEST.file]
    assert len(probe.schedules) == 2 and "--time 05:30" in probe.schedules[-1]
    rt = load_runtime(Paths(home))
    assert rt.llama.model_path.endswith(BEST.file) and Path(rt.llama.model_path).is_file()
    assert rt.llama.flags.n_cpu_moe == 12
    assert load_flags(Paths(home))["schedule_time"] == "05:30"


# ---------------------------------------------------------------------------- review round 1, finding 3
def test_the_old_model_stays_referenced_until_the_new_one_is_downloaded_and_checked(
    tmp_path: Path, probe: Probe
) -> None:
    """A finished Balanced install with its nightly task, run again with Best: the Best file takes a long time
    to download, and the nightly run at 03:00 must find a model file the whole time."""
    answers = write_answers(tmp_path, llm_tier="balanced")
    home = tmp_path / "home"
    assert main(args(tmp_path, answers)) == 0
    probe.reset()
    edit_answers(answers, llm_tier="best")
    assert main(args(tmp_path, answers)) == 0

    best_download = [model for dest, model in probe.downloads if dest.endswith(BEST.file)]
    assert best_download == [
        BALANCED.file
    ]  # while Best was being fetched runtime.json named a file that exists
    assert set(probe.before_the_first_tune()) == {BALANCED.file}  # and it did so until tuning began
    assert probe.tunes == [BEST.file]  # tuning is the first thing that sees the new model
    assert Path(load_runtime(Paths(home)).llama.model_path).name == BEST.file


def test_a_setup_closed_after_the_download_and_run_again_still_ends_on_the_new_model(
    tmp_path: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = write_answers(tmp_path, llm_tier="balanced")
    home = tmp_path / "home"
    assert main(args(tmp_path, answers)) == 0
    edit_answers(answers, llm_tier="best")
    probe.reset()

    class ClosedAtTuning(wizard.FakeRunner):  # type: ignore[misc, name-defined]
        def run(self, argv, **kw):  # type: ignore[no-untyped-def]
            if kw.get("tag") == "ns tune":
                raise Killed("the window was closed")
            return super().run(argv, **kw)

    with monkeypatch.context() as m:
        m.setattr(wizard, "FakeRunner", ClosedAtTuning)
        with pytest.raises(Killed):
            main(args(tmp_path, answers))
    assert Path(load_runtime(Paths(home)).llama.model_path).is_file()  # whatever it names, the file is there
    assert "tuned" not in load_flags(Paths(home))
    assert main(args(tmp_path, answers)) == 0
    rt = load_runtime(Paths(home))
    assert Path(rt.llama.model_path).name == BEST.file and rt.llama.flags.n_cpu_moe == 12
    assert load_flags(Paths(home))["tuned"] in ("tuned", "defaults")


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
    # the Choices screen has no field for seed keywords, so it must not wipe them (review round 1, finding 4):
    # the owner's dashboard edit stands in the yaml and the saved answers still carry the earlier seeds
    assert ch.seed_keywords == ["lighthouse"]
    saved = json.loads((home / "config" / "answers.json").read_text(encoding="utf-8"))
    assert saved["channel"]["seed_keywords"] == old["channel"]["seed_keywords"]


def test_the_window_does_not_write_the_choices_it_never_asks_for(tmp_path: Path) -> None:
    from tests.test_installer_web_answers import good_draft

    raw = answers_view.draft_to_answers(good_draft())
    assert "seed_keywords" not in raw["channel"]  # the Choices screen has no such field
    earlier = example_answers()  # seed keywords: haunted house
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=FakeUi(interactive=False))
    assert "channel.seed_keywords" not in [c.key for c in changes(ctx, earlier, raw)]


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
    )
    wordy["channel"]["language"], wordy["channel"]["seed_keywords"] = "en", ["haunted house"]
    ctx = reinstall_ctx(tmp_path, wordy)
    assert S01Questions().check(ctx) is True


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


def have_model(ctx, file: str) -> None:
    """The file is in models\\llm, as s03 leaves it once the download is verified."""
    ctx.paths.models_llm.mkdir(parents=True, exist_ok=True)
    (ctx.paths.models_llm / file).write_bytes(b"gguf")


def test_a_changed_tier_rebuilds_the_llama_part_when_the_model_is_there_and_says_what_it_kept(
    tmp_path: Path,
) -> None:
    kept = install_with(tmp_path, llm_tier="balanced")
    rt = load_runtime(kept.paths)
    rt.llama.port, rt.llama.flags.n_gpu_layers = 8085, 33  # tuning found 33 layers
    save_runtime(kept.paths, rt)
    kept.set_flag("tuned", "tuned")
    ctx = reinstall_ctx(tmp_path, example_answers(llm_tier="best"))
    step = S01Questions()
    assert step.check(ctx) is False
    step.run(ctx)
    said = ctx.ui.text
    assert "differ from your earlier install's (config\\answers.json): llm_tier: balanced -> best" in said
    assert not (ctx.paths.config / answers_view.KEPT_NAME).exists()
    assert ctx.flag("tuned") is None  # s06 tunes the new model
    # setup has not downloaded the Best file yet: runtime.json still runs the model it had
    still = load_runtime(ctx.paths)
    assert still.llama.model_path.endswith(BALANCED.file) and still.llama.flags.n_gpu_layers == 33
    assert S06Tune().check(ctx) is False  # and tuning waits for the rebuild

    have_model(ctx, BEST.file)  # s03 downloaded and verified it
    S06Tune().run(ctx)
    now = load_runtime(ctx.paths)
    assert now.llama.model_path.endswith(BEST.file) and now.llama.flags.n_gpu_layers == 99  # not the tuned 33
    assert now.llama.flags.n_cpu_moe == 12 and now.llama.flags.kv_type == "q8_0" and now.llama.port == 8085
    assert f"now uses {BEST.file} (it had {BALANCED.file})" in ctx.ui.text
    assert "stays in models\\llm" in ctx.ui.text
    assert ctx.flag(reinstall.PENDING_FLAG) is None and ctx.flag("tuned") == "tuned"


def test_runtime_json_is_not_pointed_at_a_model_that_is_not_on_disk(tmp_path: Path) -> None:
    install_with(tmp_path, llm_tier="balanced")
    ctx = reinstall_ctx(tmp_path, example_answers(llm_tier="best"))
    S01Questions().run(ctx)
    S06Tune().run(ctx)  # the Best file never arrived
    assert load_runtime(ctx.paths).llama.model_path.endswith(BALANCED.file)
    assert f"{BEST.file} is not in models\\llm yet" in ctx.ui.text
    assert ctx.flag(reinstall.PENDING_FLAG)  # the rebuild is still owed
    assert S06Tune().check(ctx) is False
    have_model(ctx, BEST.file)
    S06Tune().run(ctx)
    assert load_runtime(ctx.paths).llama.model_path.endswith(BEST.file)


def test_a_custom_story_model_replaces_the_tier_model_in_runtime_json(tmp_path: Path) -> None:
    install_with(tmp_path, llm_tier="balanced")
    ctx = reinstall_ctx(tmp_path, example_answers(llm_tier="balanced", llm_custom=CUSTOM))
    S01Questions().run(ctx)
    assert load_runtime(ctx.paths).llama.model_path.endswith(BALANCED.file)  # until the custom file is there
    have_model(ctx, CUSTOM["file"])
    S06Tune().run(ctx)
    assert load_runtime(ctx.paths).llama.model_path.endswith("some-model-Q4_K_M.gguf")
    assert load_runtime(ctx.paths).llama.flags.n_cpu_moe == 0  # a custom model: plain flags


# ---------------------------------------------------------------------------- review round 1, finding 2
def test_a_tier_in_the_new_answers_drops_the_kept_custom_model(tmp_path: Path, probe: Probe) -> None:
    """The kept answers carry a custom GGUF; the new file names only a tier. The tier wins, as in the window
    (draft_to_answers writes llm_custom null with the tier), and the install ends on the tier's model."""
    first = write_answers(tmp_path, llm_custom=CUSTOM)
    home = tmp_path / "home"
    assert main(args(tmp_path, first)) == 0
    assert Path(load_runtime(Paths(home)).llama.model_path).name == CUSTOM["file"]

    only_a_tier = tmp_path / "tier-only.json"
    only_a_tier.write_text(json.dumps({"llm_tier": "best"}), encoding="utf-8")
    probe.reset()
    assert main(args(tmp_path, only_a_tier)) == 0

    saved = json.loads((home / "config" / "answers.json").read_text(encoding="utf-8"))
    assert saved["llm_tier"] == "best" and saved["llm_custom"] is None
    assert [d for d, _ in probe.downloads if d.endswith(BEST.file)]  # the Best file really was fetched
    rt = load_runtime(Paths(home))
    assert Path(rt.llama.model_path).name == BEST.file and Path(rt.llama.model_path).is_file()
    assert rt.llama.flags.n_cpu_moe == 12 and probe.tunes == [BEST.file]
    assert (home / "models" / "llm" / CUSTOM["file"]).is_file()  # the custom file is not deleted


def test_the_custom_model_is_kept_when_the_new_answers_do_not_name_a_tier(tmp_path: Path) -> None:
    install_with(tmp_path, llm_tier="balanced", llm_custom=CUSTOM)
    ctx = reinstall_ctx(tmp_path, {"run_time": "05:00"})  # names one choice, neither tier nor model
    earlier = kept_answers(ctx)
    assert earlier is not None and [c.key for c in changes(ctx, earlier[0], ctx.raw_answers)] == ["run_time"]
    S01Questions().run(ctx)
    assert ctx.answers is not None and ctx.answers.llm_custom is not None
    assert ctx.answers.llm_custom.file == CUSTOM["file"]


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
    assert "was made for another kind of install" in ctx.ui.text
    assert load_runtime(ctx.paths).dev_mode is True  # not before the models of the new kind are there
    have_model(ctx, BALANCED.file)
    S06Tune().run(ctx)
    assert load_runtime(ctx.paths).dev_mode is False


def test_an_engine_test_answer_that_changes_reaches_runtime_json(tmp_path: Path) -> None:
    install_with(tmp_path)
    ctx = reinstall_ctx(tmp_path, example_answers(fake_voice=True, fake_footage=True))
    S01Questions().run(ctx)
    S06Tune().run(ctx)
    rt = load_runtime(ctx.paths)
    assert rt.tts.engine == "fake" and rt.fake_footage is True
    back = reinstall_ctx(tmp_path, example_answers(fake_voice=False, fake_footage=False), "back.json")
    back.set_flag("qwentts", "none")  # what s02 decided for the fake voice
    back.set_flag("tts_fallback", False)
    S01Questions().run(back)
    # review round 1, finding 7: the voice engine program was s02's decision for the old answer
    assert back.flag("qwentts") is None and back.flag("tts_fallback") is None
    S06Tune().run(back)
    assert load_runtime(back.paths).tts.engine == "qwentts" and load_runtime(back.paths).fake_footage is False


def test_a_fake_voice_that_stays_as_it_was_leaves_the_voice_engine_decision_alone(tmp_path: Path) -> None:
    install_with(tmp_path, fake_voice=True)
    ctx = reinstall_ctx(tmp_path, example_answers(fake_voice=True, run_time="05:00"))
    ctx.set_flag("qwentts", "none")
    S01Questions().run(ctx)
    assert ctx.flag("qwentts") == "none"


def test_turning_the_engine_test_voice_off_ends_on_the_real_voice_engine(
    tmp_path: Path, probe: Probe
) -> None:
    """Review round 1, finding 7, to the engine that s08 ends on: fake_voice true -> false on a finished
    install. The smoke test fails the voice checks when the engine runtime.json names is not on disk, and its
    fallback then downloads about 7 GB of PyTorch; the install must instead fetch the voice program."""
    probe.verify_needs_the_voice_program = True
    answers = write_answers(tmp_path, fake_voice=True)
    home = tmp_path / "home"
    assert main(args(tmp_path, answers)) == 0
    assert load_runtime(Paths(home)).tts.engine == "fake"
    assert not (home / "bin" / "qwentts" / "qwen-tts.exe").exists()

    edit_answers(answers, fake_voice=False)
    probe.reset()
    assert main(args(tmp_path, answers)) == 0
    rt = load_runtime(Paths(home))
    assert rt.tts.engine == "qwentts" and (home / "bin" / "qwentts" / "qwen-tts.exe").is_file()
    flags = load_flags(Paths(home))
    assert flags["qwentts"] == "binary" and flags["tts_fallback"] is False
    assert not (home / "venvs" / "tts").exists()  # no PyTorch fallback venv
    assert not [d for d, _ in probe.downloads if d.endswith(".safetensors")]  # and no 7 GB of weights
    assert [d for d, _ in probe.downloads if "/models/tts/" in d.replace("\\", "/")]  # the voice models


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
    have_model(ctx, BEST.file)
    S06Tune().run(ctx)
    assert load_runtime(ctx.paths).llama.model_path.endswith(BEST.file)
    assert load_runtime(ctx.paths).llama.flags.n_cpu_moe == 12
    # a model that is not one of setup's own (picked by hand) is left alone
    rt = load_runtime(ctx.paths)
    rt.llama.model_path = str(ctx.paths.models_llm / "my-own-finetune.gguf")
    save_runtime(ctx.paths, rt)
    (ctx.paths.config / "answers.json").unlink()
    again = make_test_context(tmp_path, raw_answers=example_answers(llm_tier="balanced"), ui=FakeUi(False))
    S01Questions().run(again)
    have_model(again, BALANCED.file)
    S06Tune().run(again)
    assert load_runtime(again.paths).llama.model_path.endswith("my-own-finetune.gguf")


def test_a_custom_model_with_a_tier_in_the_file_is_not_a_change_every_time(tmp_path: Path) -> None:
    ctx = make_test_context(tmp_path, raw_answers=example_answers(llm_tier="best", llm_custom=CUSTOM))
    S01Questions().run(ctx)
    saved = json.loads((ctx.paths.config / "answers.json").read_text(encoding="utf-8"))
    assert saved["llm_tier"] == "small"  # s01 saves the plain tier with a custom model
    again = reinstall_ctx(tmp_path, example_answers(llm_tier="best", llm_custom=CUSTOM))
    assert S01Questions().check(again) is True


# ---------------------------------------------------------------------------- review round 1, finding 1
def window_left_its_choices(tmp_path: Path, **earlier) -> tuple[Path, Path]:
    """A setup window pressed Install and stopped before s01 finished: config/answers.json holds the choices
    it wrote (best, 04:15), answers.kept.json the answers the earlier install's files were made from."""
    kept_ctx = install_with(tmp_path, **earlier)
    saved = kept_ctx.paths.config / "answers.json"
    kept = kept_ctx.paths.config / answers_view.KEPT_NAME
    kept.write_text(saved.read_text(encoding="utf-8"), encoding="utf-8")
    window = json.loads(saved.read_text(encoding="utf-8"))
    window.update(llm_tier="best", run_time="04:15")
    saved.write_text(json.dumps(window), encoding="utf-8")
    return saved, kept


def test_a_leftover_kept_file_does_not_hide_the_owners_file_from_a_headless_run(tmp_path: Path) -> None:
    """The window's choices (best) were never applied. The owner then runs setup with their original file,
    which equals the kept answers: the file wins, so s01 runs and the install stays as the file says."""
    saved, kept = window_left_its_choices(tmp_path, llm_tier="balanced", run_time="03:00")
    ctx = reinstall_ctx(tmp_path, example_answers(llm_tier="balanced", run_time="03:00"))
    step = S01Questions()
    assert step.check(ctx) is False  # the screen would have said nothing changed, and installed Best at 04:15
    step.run(ctx)
    now = json.loads(saved.read_text(encoding="utf-8"))
    assert now["llm_tier"] == "balanced" and now["run_time"] == "03:00"
    assert ctx.answers is not None and ctx.answers.llm_tier == "balanced" and ctx.answers.run_time == "03:00"
    assert not kept.exists()  # used up
    assert (
        ctx.flag(reinstall.PENDING_FLAG) is None
    )  # the files still match the earlier answers: nothing to redo
    assert "saved by a setup window that did not finish" in ctx.ui.text
    assert S01Questions().check(reinstall_ctx(tmp_path, example_answers(llm_tier="balanced"), "again.json"))


def test_a_leftover_kept_file_still_gets_the_windows_unapplied_choices_applied(tmp_path: Path) -> None:
    """The owner's file names exactly what the window had written: that is a change from what the files were
    made from (the kept answers), so it is applied, not taken for 'nothing changed'."""
    _saved, kept = window_left_its_choices(tmp_path, llm_tier="balanced", run_time="03:00")
    ctx = reinstall_ctx(tmp_path, example_answers(llm_tier="best", run_time="04:15"))
    step = S01Questions()
    assert step.check(ctx) is False
    step.run(ctx)
    assert ctx.flag(reinstall.PENDING_FLAG) and not kept.exists()
    have_model(ctx, BEST.file)
    S06Tune().run(ctx)
    assert load_runtime(ctx.paths).llama.model_path.endswith(BEST.file)


def test_a_kept_file_that_matches_the_answers_in_use_is_used_up_without_a_rerun(tmp_path: Path) -> None:
    ctx0 = install_with(tmp_path, llm_tier="balanced")
    kept = ctx0.paths.config / answers_view.KEPT_NAME
    kept.write_text((ctx0.paths.config / "answers.json").read_text(encoding="utf-8"), encoding="utf-8")
    ctx = reinstall_ctx(tmp_path, example_answers(llm_tier="balanced"))
    assert S01Questions().check(ctx) is True
    assert not kept.exists()  # a stale copy must not decide the next comparison


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


# ---------------------------------------------------------------------------- review round 1, finding 5
def test_a_key_in_the_new_answers_makes_s01_run_and_is_stored_without_being_printed(tmp_path: Path) -> None:
    """D-14 lists keys among the answers that must win. A key the key store does not hold yet is a change."""
    install_with(tmp_path)
    ctx = reinstall_ctx(tmp_path, example_answers(pexels_api_key="pk-owner-secret-1"))
    step = S01Questions()
    assert step.check(ctx) is False
    step.run(ctx)
    assert ctx.store.get("pexels_api_key") == "pk-owner-secret-1"
    assert "Pexels" in ctx.ui.text and "pk-owner-secret-1" not in ctx.ui.text
    saved = (ctx.paths.config / "answers.json").read_text(encoding="utf-8")
    assert "pk-owner-secret-1" not in saved
    # once it is stored the same file asks for nothing more
    again = reinstall_ctx(
        tmp_path, example_answers(pexels_api_key="pk-owner-secret-1"), "again.json", store=ctx.store
    )
    assert S01Questions().check(again) is True
    other = reinstall_ctx(
        tmp_path, example_answers(pexels_api_key="pk-owner-secret-2"), "other.json", store=ctx.store
    )
    assert S01Questions().check(other) is False  # a different key is a new key


def test_a_key_in_the_file_after_uninstall_keep_my_data_is_stored_and_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The uninstall deletes secrets.dpapi and forgets the keys; the file next to INSTALL.cmd carries a new
    one. It used to be erased from the file without being stored, with a line saying it was in the keyring."""
    home, answers = a_home_that_kept_its_data(tmp_path)
    keyring = MemoryStore()  # one key store for the run, so the test can look into it afterwards
    monkeypatch.setattr(wizard, "MemoryStore", lambda: keyring)
    edit_answers(answers, pexels_api_key="pk-owner-secret-3")
    assert main(args(tmp_path, answers)) == 0
    assert keyring.get("pexels_api_key") == "pk-owner-secret-3"  # stored ...
    assert (
        json.loads(answers.read_text(encoding="utf-8"))["pexels_api_key"] == ""
    )  # ... and only then cleared
    for f in home.rglob("*"):
        if f.is_file() and f.suffix in (".json", ".jsonl", ".yaml"):
            assert "pk-owner-secret-3" not in f.read_text(encoding="utf-8", errors="replace"), f


def test_s07_never_clears_a_key_from_the_owners_file_that_the_key_store_does_not_hold(tmp_path: Path) -> None:
    """Belt and braces for any path that gets to s07 without s01 having stored the key."""
    from installer.stage2.steps.s07_configs import S07Configs

    ctx = install_with(tmp_path)
    mine = tmp_path / "owner-answers.json"
    mine.write_text(json.dumps(example_answers(pixabay_api_key="xk-owner-secret-4")), encoding="utf-8")
    ctx.answers_path = mine
    assert ctx.store.get("pixabay_api_key") is None
    S07Configs().run(ctx)
    assert ctx.store.get("pixabay_api_key") == "xk-owner-secret-4"
    assert json.loads(mine.read_text(encoding="utf-8"))["pixabay_api_key"] == ""


# ---------------------------------------------------------------------------- the smaller notes of the review
def test_a_resumed_install_does_not_claim_there_was_an_earlier_install(tmp_path: Path) -> None:
    install_with(tmp_path, llm_tier="balanced")
    resumed = make_test_context(tmp_path, ui=FakeUi(interactive=False))
    resumed.answers_path = resumed.paths.config / "answers.json"
    resumed.raw_answers = json.loads(resumed.answers_path.read_text(encoding="utf-8"))
    resumed.resumed = True  # this install already finished a step: the files are its own
    step = S01Questions()
    assert step.check(resumed) is True
    step.verify(resumed)
    assert "earlier install" not in resumed.ui.text and "Kept" not in resumed.ui.text


def test_a_new_display_name_that_keeps_the_slug_reaches_the_channel_yaml(tmp_path: Path) -> None:
    install_with(tmp_path)
    new = example_answers()
    new["channel"] = {"display_name": "AFTER DARK TALES", "niche": "horror fiction"}  # the same slug
    ctx = reinstall_ctx(tmp_path, new)
    S01Questions().run(ctx)
    assert load_channel(ctx.paths, "after-dark-tales").display_name == "AFTER DARK TALES"
    assert "your answers changed display name" in ctx.ui.text


def test_a_failing_apply_leaves_the_saved_answers_alone_so_a_rerun_still_sees_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_with(tmp_path, llm_tier="balanced")
    ctx = reinstall_ctx(tmp_path, example_answers(llm_tier="best"))

    def boom(*_a: object, **_k: object) -> list[str]:
        raise RuntimeError("the channel yaml does not load")

    monkeypatch.setattr(reinstall, "apply", boom)
    with pytest.raises(RuntimeError):
        S01Questions().run(ctx)
    saved = json.loads((ctx.paths.config / "answers.json").read_text(encoding="utf-8"))
    assert saved["llm_tier"] == "balanced"
    rerun = reinstall_ctx(tmp_path, example_answers(llm_tier="best"), "again.json")
    assert S01Questions().check(rerun) is False


def test_the_tiny_story_answer_reaches_runtime_json_and_goes_back_out(tmp_path: Path) -> None:
    """H-8: `tiny_story` is an engine-test answer like `fake_voice`: a reinstall that changes it applies."""
    install_with(tmp_path)
    ctx = reinstall_ctx(tmp_path, example_answers(tiny_story=True, allow_cpu=True))
    S01Questions().run(ctx)
    assert "tiny_story" in ctx.ui.text  # setup says which answer changed
    S06Tune().run(ctx)
    assert load_runtime(ctx.paths).tiny_story is True
    back = reinstall_ctx(tmp_path, example_answers(tiny_story=False, allow_cpu=True), "back.json")
    S01Questions().run(back)
    S06Tune().run(back)
    assert load_runtime(back.paths).tiny_story is False
