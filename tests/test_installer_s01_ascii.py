"""C1: the console install-folder question refuses a name the model programs cannot open (NS-E117)."""

from pathlib import Path

import pytest

from installer.stage2.errors import make_error
from installer.stage2.steps.s01_questions import S01Questions
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from ns_common.errors import NsError


def test_a_provided_non_ascii_home_is_ns_e117(tmp_path: Path):
    raw = example_answers(home_path="D:\caf\u00e9")
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=FakeUi(interactive=False))
    with pytest.raises(NsError) as ei:
        S01Questions().run(ctx)
    assert ei.value.code == "NS-E117" and "plain English letters" in ei.value.detail


def test_the_interactive_question_asks_again(tmp_path: Path):
    good = tmp_path / "home"
    raw = example_answers()
    raw.pop("home_path", None)
    ui = FakeUi({"Install folder": ["D:\\u0646\u0627\u064a\u062a", str(good)]})
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=ui, home_forced=False, unattended=False)
    assert S01Questions()._pick_home(ctx, "small") == good
    assert any("plain English letters" in line for line in ui.said)


def test_ns_e117_is_defined_and_has_window_copy():
    err = make_error("NS-E117", "x")
    assert err.definition.message and "plain English" in err.definition.hint
