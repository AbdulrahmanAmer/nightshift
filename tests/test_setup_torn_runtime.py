"""Setup run again over a damaged config\\runtime.json (D-35 follow-up: setup-torn-runtime).

The wizard is run for real with the real step ``s06_tune`` (the step that counts runtime.json as done). Each
test is one row of the table in the report: file fine, missing, not JSON, JSON that fails the model, damaged
with a `.bad` already there, a rename the system refuses, killed between the rename and the rewrite, and a
second run. What the owner sees is read from the rows the window would get (``step_status``), the console
lines, the state file and ``logs\\install-report.json``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from installer.stage2 import runtime_cfg
from installer.stage2.runtime_cfg import build_runtime
from installer.stage2.steps.s06_tune import S06Tune
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.wizard import EXIT_FAILED, EXIT_OK, Wizard
from ns_common.config import load_install_state, load_runtime, save_runtime
from ns_common.errors import NsError

SENTENCE = (
    "Your runtime.json could not be read; it was kept as {name} and a new one was written from your answers; "
    "settings that were only in the old file are not carried over."
)
TORN = '{"profile": "gpu", "llama": {'            # a write that stopped half way
NOT_THE_MODEL = '{"profile": "gpu", "no_such_setting": 1}'     # JSON, but Runtime is strict


class RowUi(FakeUi):
    """The rows the setup window gets: (step id, status, detail)."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, str, str | None]] = []

    def step_status(self, step_id: str, status: str, title: str, detail: str | None = None) -> None:
        self.rows.append((step_id, status, detail))

    def last(self) -> tuple[str, str, str | None]:
        return self.rows[-1]


class Killed(BaseException):
    """A power cut or a closed window."""


def context(tmp_path: Path) -> Any:
    return make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=RowUi())


def setup_again(ctx: Any) -> int:
    """One run of setup over this home (the wizard with the step that decides runtime.json)."""
    return Wizard(ctx, [S06Tune()], backoff=()).run()


def kept(ctx: Any) -> list[str]:
    return sorted(p.name for p in ctx.paths.config.glob("runtime.json.bad*"))


def report(ctx: Any) -> dict[str, Any]:
    return json.loads((ctx.paths.logs / "install-report.json").read_text(encoding="utf-8"))


def finished_install(tmp_path: Path) -> tuple[Any, bytes]:
    """A home setup finished in: runtime.json written and tuned, s06 done, flag set. Returns it, the bytes."""
    ctx = context(tmp_path)
    assert setup_again(ctx) == EXIT_OK
    assert ctx.flag("tuned") == "tuned" and kept(ctx) == []
    return ctx, ctx.paths.runtime_file.read_bytes()


def damage(ctx: Any, text: str) -> None:
    ctx.paths.runtime_file.write_text(text, encoding="utf-8")


def mended(ctx: Any, ui: RowUi, old: str, name: str = "runtime.json.bad") -> None:
    """The file is the baseline of the answers again, the old bytes are kept, and the owner was told."""
    assert load_runtime(ctx.paths).llama.model_path == build_runtime(ctx).llama.model_path
    assert (ctx.paths.config / name).read_text(encoding="utf-8") == old
    said = SENTENCE.format(name=name)
    assert ui.last() == ("s06_tune", "warning", said)                      # the step's row
    assert said in ui.text                                                 # the console line
    note = report(ctx)["steps"]["s06_tune"]
    assert note["status"] == "done" and note["note"] == said and note["error"] is None   # install-report.json
    assert load_install_state(ctx.paths).steps["s06_tune"].status == "done"


# ------------------------------------------------------------------------------------------- the rows
def test_row_file_fine_is_not_touched_and_says_nothing(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    save_runtime(ctx.paths, build_runtime(ctx))
    before = ctx.paths.runtime_file.read_bytes()
    assert setup_again(ctx) == EXIT_OK
    assert ctx.paths.runtime_file.read_bytes() == before and kept(ctx) == []
    assert ctx.ui.last() == ("s06_tune", "done", None) and "kept as" not in ctx.ui.text
    assert "note" not in report(ctx)["steps"]["s06_tune"]


def test_row_missing_is_written_again_as_before(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    ctx.paths.runtime_file.unlink()
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert setup_again(again) == EXIT_OK
    assert load_runtime(again.paths).llama.model_path == build_runtime(again).llama.model_path
    assert kept(again) == [] and ui.last() == ("s06_tune", "done", None) and "kept as" not in ui.text
    assert "note" not in report(again)["steps"]["s06_tune"]


def test_row_not_json_is_kept_as_bad_and_setup_goes_on(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    damage(ctx, TORN)
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert setup_again(again) == EXIT_OK            # the base stops, or calls the damaged file done
    assert kept(again) == ["runtime.json.bad"]
    mended(again, ui, TORN)


def test_row_json_that_fails_the_model_is_kept_as_bad_too(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    damage(ctx, NOT_THE_MODEL)
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert setup_again(again) == EXIT_OK
    assert kept(again) == ["runtime.json.bad"]
    mended(again, ui, NOT_THE_MODEL)


def test_row_the_reviewers_path_flag_says_tuned_but_the_step_is_not_marked_done(tmp_path: Path) -> None:
    """D-35's description: the step's check counted the damaged file as done (flag + file exists), skipped its
    work, and the final check failed for ever. The decision is made in the step's check."""
    ctx, _ = finished_install(tmp_path)
    ctx.state.mark("s06_tune", "pending")
    damage(ctx, TORN)
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert S06Tune().check(again) is False
    assert setup_again(again) == EXIT_OK
    mended(again, ui, TORN)


def test_row_a_bad_copy_is_already_there_both_are_kept(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    older = ctx.paths.config / "runtime.json.bad"
    older.write_text("an older damaged copy", encoding="utf-8")
    damage(ctx, TORN)
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert setup_again(again) == EXIT_OK
    assert kept(again) == ["runtime.json.bad", "runtime.json.bad.1"]
    assert older.read_text(encoding="utf-8") == "an older damaged copy"       # never overwritten
    mended(again, ui, TORN, "runtime.json.bad.1")
    # and the next damage counts on
    damage(again, NOT_THE_MODEL)
    third = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=RowUi())
    assert setup_again(third) == EXIT_OK
    assert kept(third) == ["runtime.json.bad", "runtime.json.bad.1", "runtime.json.bad.2"]
    assert (third.paths.config / "runtime.json.bad.2").read_text(encoding="utf-8") == NOT_THE_MODEL
    assert (third.paths.config / "runtime.json.bad.1").read_text(encoding="utf-8") == TORN


@pytest.mark.skipif(sys.platform != "win32", reason="a file held open is refused a rename only on Windows")
def test_row_rename_refused_names_the_file_says_what_to_close_and_changes_nothing(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    damage(ctx, TORN)
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    with open(again.paths.runtime_file, "rb"):      # a program has the file open: Windows refuses the rename
        assert setup_again(again) == EXIT_FAILED
        row = load_install_state(again.paths).steps["s06_tune"]
        assert row.status == "failed" and row.error is not None and row.error.startswith("NS-E125")
        assert str(again.paths.runtime_file) in row.error and "Close any program" in row.error
        assert "Retry" in row.error and "has not been changed" in row.error
        assert again.paths.runtime_file.read_text(encoding="utf-8") == TORN       # as it was
        assert kept(again) == []                                                  # nothing half done
        assert "kept as" not in ui.text
        assert report(again)["last_error"]["code"] == "NS-E125"
    # the owner closed it and chose Retry: the same run goes on
    ui2 = RowUi()
    retry = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui2)
    assert setup_again(retry) == EXIT_OK
    mended(retry, ui2, TORN)


def test_row_refusal_by_the_system_is_an_ns_e125_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same row without the Windows trick: whatever OSError the rename raises (a read-only folder)."""
    ctx = context(tmp_path)
    damage(ctx, TORN)

    def refuse(src: Any, dst: Any) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(runtime_cfg, "_rename", refuse)
    seen: Exception | None = None
    try:
        runtime_cfg.ensure_baseline(ctx)
    except Exception as e:      # noqa: BLE001 - the assertion below names what was raised
        seen = e
    assert isinstance(seen, NsError) and seen.code == "NS-E125", repr(seen)
    assert str(ctx.paths.runtime_file) in seen.detail and "Close any program" in seen.detail
    assert ctx.paths.runtime_file.read_text(encoding="utf-8") == TORN and kept(ctx) == []
    assert ctx.take_notes() == []


def test_row_killed_between_the_rename_and_the_rewrite_the_rerun_finds_it_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = finished_install(tmp_path)
    damage(ctx, TORN)
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)

    def die(paths: Any, rt: Any) -> None:
        raise Killed()

    with monkeypatch.context() as m:
        m.setattr(runtime_cfg, "save_runtime", die)
        with pytest.raises(Killed):
            setup_again(again)
    assert not again.paths.runtime_file.exists() and kept(again) == ["runtime.json.bad"]
    assert "kept as runtime.json.bad" in ui.text                    # the owner had already been told
    ui2 = RowUi()
    rerun = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui2)
    assert setup_again(rerun) == EXIT_OK                              # missing: rewritten as today
    assert load_runtime(rerun.paths).llama.model_path == build_runtime(rerun).llama.model_path
    assert kept(rerun) == ["runtime.json.bad"]                      # the kept copy is still the only one
    assert (rerun.paths.config / "runtime.json.bad").read_text(encoding="utf-8") == TORN
    assert ui2.last() == ("s06_tune", "done", None)


def test_row_a_second_run_over_the_mended_file_changes_nothing(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    damage(ctx, TORN)
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=RowUi())
    assert setup_again(again) == EXIT_OK
    after = again.paths.runtime_file.read_bytes()
    ui = RowUi()
    third = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert setup_again(third) == EXIT_OK
    assert third.paths.runtime_file.read_bytes() == after
    assert kept(third) == ["runtime.json.bad"]
    assert ui.last() == ("s06_tune", "done", "already complete") and "kept as" not in ui.text


def test_row_the_decision_is_in_the_check_not_a_special_case_before_it(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    assert S06Tune().check(ctx) is True
    damage(ctx, TORN)
    assert S06Tune().check(ctx) is False
    damage(ctx, NOT_THE_MODEL)
    assert S06Tune().check(ctx) is False
    ctx.paths.runtime_file.unlink()
    assert S06Tune().check(ctx) is False


def test_s01_over_kept_data_does_not_crash_on_the_damaged_file(tmp_path: Path) -> None:
    """s01 compares the answers with the kept runtime.json (``reinstall._runtime``) before s06 ever runs."""
    from installer.stage2 import reinstall

    ctx = context(tmp_path)
    damage(ctx, TORN)
    lines: list[str] | None = None
    raised: Exception | None = None
    try:
        lines = reinstall._runtime(ctx, {"llm_tier"}, True)
    except Exception as e:      # noqa: BLE001 - the assertion below names what was raised
        raised = e
    assert raised is None and lines == [], repr(raised)
    assert ctx.paths.runtime_file.read_text(encoding="utf-8") == TORN       # s01 does not move it: s06 does
