# ruff: noqa: E501, F811
"""D-40: an unattended setup never waits at a question, and a key the answers name is never asked again.

Entry: `installer.stage2.wizard.build_context` with the parsed command line of each route (the console's
`isatty` patched as a console, or `< NUL`, gives it), then the wizard's own step objects over
`make_test_context` with the fake runner and a Ui that LOGS every prompt (`PromptLog`: a prompt is a failure,
and the test says which one). The Ui the wizard builds decides whether a person can answer: the tests take its
`interactive` from the context `build_context` made, never from the test.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from installer.stage2.base import Step
from installer.stage2.secrets import PEXELS_KEY, MemoryStore
from installer.stage2.steps import default_steps
from installer.stage2.steps.s01_questions import NIGHTLY_QUESTION, NO_KEY_NOTICE
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.wizard import EXIT_FAILED, EXIT_OK, Wizard, build_context, parse_args
from tests.test_nightly_optional import FakeWindows, windows  # noqa: F401  (the fake Task Scheduler)
from tests.test_second_install import install_at, put_shortcuts
from tests.test_setup_nightly_question import SetupRunner, saved_answers, setup

ROOT = Path(__file__).resolve().parents[1]
STEPS_DIR = ROOT / "installer" / "stage2" / "steps"
PROMPTS = ("ask", "confirm", "choose", "pause")


class PromptLog(FakeUi):
    """Records every prompt a step makes (the scripted answer, else the default, as `FakeUi` gives it)."""

    def __init__(self, script: dict[str, str | list[str]] | None = None, *, interactive: bool = True) -> None:
        super().__init__(script, interactive=interactive)
        self.prompts: list[str] = []

    def ask(self, prompt: str, default: str | None = None, *, secret: bool = False) -> str:
        self.prompts.append(prompt)
        return super().ask(prompt, default, secret=secret)

    def confirm(self, prompt: str, default: bool = True) -> bool:
        self.prompts.append(prompt)
        return super().confirm(prompt, default)

    def choose(self, prompt: str, options: Any, default: str | None = None) -> str:
        self.prompts.append(prompt)
        return super().choose(prompt, options, default)

    def pause(self, prompt: str = "Press Enter to continue") -> None:
        self.prompts.append(prompt)
        super().pause(prompt)


class Console:
    """`sys.stdin` as a console (or `< NUL` on Windows, which reports a tty too) gives it to the wizard."""

    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def command_line(tmp_path: Path, answers: Path, *extra: str) -> list[str]:
    return [
        "--simulate-downloads", "--no-head-checks", "--home", str(tmp_path / "home"), "--answers", str(answers),
        "--app-path", str(ROOT), "--preflight", str(tmp_path / "no-preflight.json"), *extra,
    ]


def write_answers(where: Path, raw: dict[str, Any]) -> Path:
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(raw), encoding="utf-8")
    return where


def built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: dict[str, Any], *, config: bool = False,
    tty: bool = True, ui: PromptLog | None = None, store: MemoryStore | None = None,
    http: Any = None, extra: tuple[str, ...] = (), base: Path | None = None, desktop: Path | None = None,
) -> tuple[Any, Any]:
    """(what `build_context` made for this route, the context the steps run over). The steps' Ui gets its
    `interactive` from the built context; the rest is the test's fakes, with the real key path (`simulate`
    off) so that the key questions are reached."""
    base = base or tmp_path
    monkeypatch.setattr("installer.stage2.wizard.sys.stdin", Console(tty))
    home = base / "home"
    file = write_answers(home / "config" / "answers.json" if config else base / "answers.json", raw)
    made = build_context(parse_args(command_line(base, file, *extra)))
    log = ui or PromptLog()
    log.interactive = made.ui.interactive
    ctx = make_test_context(
        base, raw_answers=made.raw_answers, ui=log, runner=SetupRunner(desktop or tmp_path / "desktop"),
        store=store, http=http, simulate=False, unattended=made.unattended,
    )
    ctx.answers_path = made.answers_path
    return made, ctx


def no_keys(**over: Any) -> dict[str, Any]:
    """Answers the way an engine-test run names them: both keys present and empty."""
    return example_answers(install_code=False, pexels_api_key="", pixabay_api_key="", **over)


def status(code: int) -> Any:
    return lambda: httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(code)))


# ---------------------------------------------------------------------------------------------------- 1
ROUTES = {  # route -> (the answers come from config/answers.json, a console is attached, extra switches)
    "answers-file-and-a-console": (False, True, ()),
    "non-interactive-switch-and-a-console": (False, True, ("--non-interactive",)),
    "answers-file-and-no-console": (False, False, ()),
}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_an_unattended_run_with_a_console_attached_never_prompts(tmp_path: Path, monkeypatch, windows: FakeWindows, route: str):
    config, tty, extra = ROUTES[route]
    made, ctx = built(tmp_path, monkeypatch, no_keys(), config=config, tty=tty, extra=extra)
    assert made.unattended and not made.ui.interactive, "an unattended run keeps a Ui that can prompt"
    text = setup(ctx, only=("s01",))
    assert ctx.ui.prompts == []
    assert NO_KEY_NOTICE in text
    assert ctx.store.get(PEXELS_KEY) is None


# ---------------------------------------------------------------------------------------------------- 2
def test_a_key_the_answers_name_as_empty_is_not_asked_at_a_console(tmp_path: Path, monkeypatch, windows: FakeWindows):
    """The answers in `config/answers.json` (a console run: a person is there) name both keys as empty and
    leave the channel out: the channel is asked, the keys are not."""
    raw = {"llm_tier": "small", "pexels_api_key": "", "pixabay_api_key": "", "install_code": False, "nightly": False}
    ui = PromptLog({"Channel name": "After Dark Tales", "Channel niche": "horror fiction"})
    made, ctx = built(tmp_path, monkeypatch, raw, config=True, ui=ui)
    assert not made.unattended and made.ui.interactive  # a person can answer here
    text = setup(ctx, only=("s01",))
    assert [p for p in ui.prompts if "API key" in p or "Pexels" in p or "Pixabay" in p] == []
    assert any("Channel name" in p for p in ui.prompts)  # the console still asks what the answers leave out
    assert NO_KEY_NOTICE in text
    assert saved_answers(ctx.paths)["pexels_api_key"] in ("", None)  # no key is ever written to the file


def test_a_key_the_answers_do_not_name_is_asked_at_a_console(tmp_path: Path, monkeypatch, windows: FakeWindows):
    raw = {"llm_tier": "small", "install_code": False, "nightly": False}
    ui = PromptLog({"Channel name": "After Dark Tales", "Channel niche": "horror fiction"})
    made, ctx = built(tmp_path, monkeypatch, raw, config=True, ui=ui)
    assert made.ui.interactive
    setup(ctx, only=("s01",))
    asked = [p for p in ui.prompts if "API key" in p]
    assert asked == ["Paste your Pexels API key", "Paste your Pixabay API key"]


@pytest.mark.parametrize("accepted", [True, False])
def test_a_stored_key_is_still_checked_when_the_answers_name_the_key_as_empty(tmp_path: Path, monkeypatch, windows: FakeWindows, accepted: bool):
    store = MemoryStore()
    store.set(PEXELS_KEY, "saved-key")
    made, ctx = built(tmp_path, monkeypatch, no_keys(), store=store, http=status(200 if accepted else 401))
    text = setup(ctx, only=("s01",))
    assert ctx.ui.prompts == []
    assert (store.get(PEXELS_KEY) == "saved-key") is accepted
    assert ("The saved Pexels key was not accepted" in text) is (not accepted)
    assert (NO_KEY_NOTICE in text) is (not accepted)  # a kept key means a key is stored: no notice


def test_a_models_folder_the_answers_name_as_blank_is_not_asked_at_a_console(tmp_path: Path, monkeypatch, windows: FakeWindows):
    raw = {"llm_tier": "small", "models_source": "", "pexels_api_key": "", "pixabay_api_key": "", "install_code": False, "nightly": False}
    ui = PromptLog({"Channel name": "After Dark Tales", "Channel niche": "horror fiction"})
    made, ctx = built(tmp_path, monkeypatch, raw, config=True, ui=ui)
    assert made.ui.interactive and made.raw_answers["models_source"] is None  # `load_raw` reads "" as None
    setup(ctx, only=("s01",))
    assert [p for p in ui.prompts if "model files" in p] == []
    assert ctx.answers is not None and ctx.answers.models_source is None


# ---------------------------------------------------------------------------------------------------- 3
def test_the_headless_setup_server_starts_the_wizard_non_interactive(tmp_path: Path, monkeypatch):
    from installer.stage2.web import answers_view, controller
    from tests.test_installer_web_controller import Setup, draft

    s = Setup(tmp_path, monkeypatch)
    s.cfg.simulate = False  # the real route: only the simulated run used to pass the switch
    answers = tmp_path / "answers.json"
    answers_view.write_answers(answers, answers_view.draft_to_answers(draft(s.home)))
    s.cfg.answers, s.cfg.headless = answers, True
    seen: list[list[str]] = []

    def fake_run(argv, **kw):
        seen.append([str(a) for a in argv])
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    assert s.ctl.run_headless() == 0
    wizard = [a for a in seen if "installer.stage2.wizard" in a]
    assert len(wizard) == 1 and "--non-interactive" in wizard[0] and "--simulate-downloads" not in wizard[0]


# ---------------------------------------------------------------------------------------------------- 4
def test_the_nightly_question_is_not_asked_in_an_unattended_run(tmp_path: Path, monkeypatch, windows: FakeWindows):
    raw = no_keys()
    del raw["install_code"]
    made, ctx = built(tmp_path, monkeypatch, raw)  # no `nightly`, a console attached
    assert made.unattended
    setup(ctx, only=("s01",))
    assert NIGHTLY_QUESTION not in ctx.ui.prompts and ctx.ui.prompts == []
    assert saved_answers(ctx.paths).get("nightly") in (None, False)
    assert ctx.answers is not None and not ctx.answers.nightly


def test_the_nightly_question_is_asked_at_a_console_without_an_answers_file(tmp_path: Path, monkeypatch, windows: FakeWindows):
    """The other side: a person at the console with no answers file in use is asked, once, and Enter is no."""
    raw = {"llm_tier": "small", "pexels_api_key": "", "pixabay_api_key": ""}
    ui = PromptLog({"Channel name": "After Dark Tales", "Channel niche": "horror fiction"})
    made, ctx = built(tmp_path, monkeypatch, raw, config=True, ui=ui)
    assert made.ui.interactive
    setup(ctx, only=("s01",))
    assert ui.prompts.count(NIGHTLY_QUESTION) == 1
    assert not ctx.answers.nightly


def test_the_shortcuts_question_is_not_asked_in_an_unattended_run(tmp_path: Path, monkeypatch, windows: FakeWindows):
    a = install_at(tmp_path / "A")
    desktop = tmp_path / "desktop"
    before = put_shortcuts(desktop, a)
    made, ctx = built(tmp_path, monkeypatch, no_keys(), base=tmp_path / "b", desktop=desktop)
    assert made.unattended
    setup(ctx, only=("s01",))
    assert ctx.ui.prompts == []
    assert {p.name: p.read_bytes() for p in sorted(desktop.glob("*.lnk"))} == before
    assert "shortcuts_here" not in saved_answers(ctx.paths) or not saved_answers(ctx.paths)["shortcuts_here"]


# ---------------------------------------------------------------------------------------------------- 5
def prompting_steps() -> dict[str, str]:
    """Step file -> the prompts its source makes: every `ui.ask`, `ui.confirm`, `ui.choose` and `ui.pause` in
    `installer/stage2/steps/*.py`, found by reading the code."""
    found: dict[str, str] = {}
    for path in sorted(STEPS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = {
            n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in PROMPTS and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "ui"
        }
        if calls:
            found[path.stem] = ",".join(sorted(calls))
    return found


def test_every_step_that_prompts_is_exercised_by_the_unattended_run_below():
    """A prompt added to another step is not covered until its id is added here AND the run below reaches it."""
    assert set(prompting_steps()) == {"s00_welcome", "s01_questions"}, prompting_steps()


@pytest.mark.parametrize("route", ["answers-file", "no-console"])
def test_no_step_prompts_in_an_unattended_run(tmp_path: Path, monkeypatch, route: str):
    """The whole wizard (every step, the failure menu included) as `INSTALL.cmd -Answers ... -NoUi` runs it
    with a console attached (or none), over an answers file that leaves out the optional questions."""
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)
    raw = example_answers(llm_tier="small", home_path=str(tmp_path / "home"))
    raw.pop("install_code")
    file = write_answers(tmp_path / "answers.json", raw)
    monkeypatch.setattr("installer.stage2.wizard.sys.stdin", Console(route == "answers-file"))
    ctx = build_context(parse_args(command_line(tmp_path, file)))
    assert ctx.unattended and not ctx.ui.interactive
    ui = ctx.ui = PromptLog(interactive=ctx.ui.interactive)  # the Ui the wizard built decides; this one logs
    assert Wizard(ctx, default_steps(), backoff=(0, 0, 0)).run() == EXIT_OK
    assert ui.prompts == []


class Failing(Step):
    id, title = "x_fail", "A step that fails"

    def check(self, ctx) -> bool:
        return False

    def run(self, ctx) -> None:
        from installer.stage2.errors import make_error

        raise make_error("NS-E105", "boom")

    def verify(self, ctx) -> None:
        return None


def test_a_failed_step_in_an_unattended_run_retries_and_gives_up_without_a_menu(tmp_path: Path, monkeypatch):
    raw = example_answers(llm_tier="small", home_path=str(tmp_path / "home"))
    file = write_answers(tmp_path / "answers.json", raw)
    monkeypatch.setattr("installer.stage2.wizard.sys.stdin", Console(True))
    ctx = build_context(parse_args(command_line(tmp_path, file)))
    assert ctx.unattended and not ctx.ui.interactive
    ui = ctx.ui = PromptLog(interactive=ctx.ui.interactive)
    assert Wizard(ctx, [Failing()], backoff=(0, 0, 0)).run() == EXIT_FAILED
    assert ui.prompts == [] and "Retry 3 of 3" in ui.text
