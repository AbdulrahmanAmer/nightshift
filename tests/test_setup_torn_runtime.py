"""Setup run again over a damaged config\\runtime.json (D-35 follow-up: setup-torn-runtime).

The wizard is run for real with the real step ``s06_tune`` (the step that counts runtime.json as done). Each
test is one row of the table in the report: file fine, missing, not JSON, JSON that fails the model, damaged
with a `.bad` already there, a rename the system refuses, killed between the rename and the rewrite, and a
second run. What the owner sees is read from the rows the window would get (``step_status``), the console
lines, the state file and ``logs\\install-report.json``.

The second half is the table of the one mechanism: the state of the existing file (fine, byte order mark,
UTF-16, unknown settings, missing, damaged, unreadable, vanishing) x what the answers changed (nothing, the
tier, dev to real, real to dev, the engine test), through the real steps s01, s03, s06 and s07. Every cell is
held to one invariant (``expected``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from installer.stage2 import runtime_cfg
from installer.stage2.base import Step
from installer.stage2.runtime_cfg import build_runtime, update_runtime
from installer.stage2.steps.s06_tune import S06Tune
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.wizard import EXIT_FAILED, EXIT_OK, Wizard
from ns_common.config import load_install_state, load_runtime, save_runtime
from ns_common.errors import NsError
from ns_common.paths import Paths

SENTENCE = (
    "Your runtime.json could not be read; it was kept as {name} and a new one was written from your answers; "
    "settings that were only in the old file are not carried over."
)
TORN = '{"profile": "gpu", "llama": {'            # a write that stopped half way
NOT_THE_MODEL = '{"profile": "gpu", "supervisor_port": "not a port"}'     # JSON, a known field, bad value


class RowUi(FakeUi):
    """The rows the setup window gets: (step id, status, detail)."""

    def __init__(self, *, interactive: bool = True) -> None:
        super().__init__(interactive=interactive)
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


# ------------------------------------------------------------------------- a file a NEWER Nightshift wrote
def newer_file(ctx: Any, **top: Any) -> tuple[str, dict[str, Any]]:
    """runtime.json as a newer Nightshift would leave it: this version's file with the owner's own port and
    model, plus fields this version does not have (one at the top, one nested). Returns its text and dict."""
    data = json.loads(ctx.paths.runtime_file.read_text(encoding="utf-8"))
    data["supervisor_port"] = 8123
    data["llama"]["model_path"] = "D:/models/owners-own.gguf"
    data["future_setting"] = {"a": 1}
    data["llama"]["flags"]["future_flag"] = 3
    data.update(top)
    text = json.dumps(data, indent=2)
    ctx.paths.runtime_file.write_text(text, encoding="utf-8")
    return text, data


def newer_kept(ctx: Any) -> list[str]:
    return sorted(p.name for p in ctx.paths.config.glob("runtime.json.newer*"))


def test_newer_file_only_unknown_fields_keeps_every_known_value_and_the_original(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    text, _data = newer_file(ctx)
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert setup_again(again) == EXIT_OK
    now = load_runtime(again.paths)
    assert now.supervisor_port == 8123 and now.llama.model_path == "D:/models/owners-own.gguf"   # kept
    on_disk = json.loads(again.paths.runtime_file.read_text(encoding="utf-8"))
    assert "future_setting" not in on_disk and "future_flag" not in on_disk["llama"]["flags"]   # only those
    assert newer_kept(again) == ["runtime.json.newer"] and kept(again) == []
    assert (again.paths.config / "runtime.json.newer").read_text(encoding="utf-8") == text
    said = ui.last()
    assert said[:2] == ("s06_tune", "warning") and said[2] is not None
    for part in ("has settings this version does not know", "a newer Nightshift, or a hand edit",
                 "future_setting", "llama.flags.future_flag", "they were removed",
                 "every other setting was kept", "runtime.json.newer", "Tuning runs again"):
        assert part in said[2], part
    assert "could not be read" not in said[2] and "was written by a newer" not in said[2]
    assert report(again)["steps"]["s06_tune"]["note"] == said[2]


def test_newer_file_with_a_nested_unknown_field_only(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    data = json.loads(ctx.paths.runtime_file.read_text(encoding="utf-8"))
    data["llama"]["flags"]["future_flag"] = 3
    ctx.paths.runtime_file.write_text(json.dumps(data), encoding="utf-8")
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert setup_again(again) == EXIT_OK
    said = ui.last()[2] or ""
    assert "llama.flags.future_flag" in said and "future_setting" not in said
    del data["llama"]["flags"]["future_flag"]
    assert json.loads(again.paths.runtime_file.read_text(encoding="utf-8"))["llama"] == data["llama"]


def test_newer_file_a_second_run_changes_nothing_and_a_second_newer_file_is_kept_beside_the_first(
    tmp_path: Path,
) -> None:
    ctx, _ = finished_install(tmp_path)
    first_text, _ = newer_file(ctx)
    once = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=RowUi())
    assert setup_again(once) == EXIT_OK
    after = once.paths.runtime_file.read_bytes()
    ui = RowUi()
    twice = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert setup_again(twice) == EXIT_OK
    assert twice.paths.runtime_file.read_bytes() == after and newer_kept(twice) == ["runtime.json.newer"]
    assert ui.last() == ("s06_tune", "done", "already complete")
    newer_file(twice)       # a newer Nightshift writes it again
    third = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=RowUi())
    assert setup_again(third) == EXIT_OK
    assert newer_kept(third) == ["runtime.json.newer", "runtime.json.newer.1"]
    assert (third.paths.config / "runtime.json.newer").read_text(encoding="utf-8") == first_text


def test_both_kinds_of_complaint_is_damaged_and_the_message_names_both(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    text, _ = newer_file(ctx, supervisor_port="not a port")
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert setup_again(again) == EXIT_OK
    assert kept(again) == ["runtime.json.bad"] and newer_kept(again) == []
    assert (again.paths.config / "runtime.json.bad").read_text(encoding="utf-8") == text
    assert load_runtime(again.paths).supervisor_port == build_runtime(again).supervisor_port    # the baseline
    said = ui.last()[2] or ""
    assert said.startswith(SENTENCE.format(name="runtime.json.bad")[:-1])
    for part in ("future_setting", "llama.flags.future_flag", "supervisor_port"):
        assert part in said, part


def test_a_file_that_cannot_be_opened_is_ns_e125_not_a_bare_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = context(tmp_path)
    damage(ctx, TORN)

    def held(path: Any) -> bytes:
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(runtime_cfg, "read_bytes_retry", held)
    seen: Exception | None = None
    try:
        runtime_cfg.ensure_baseline(ctx)
    except Exception as e:      # noqa: BLE001 - the assertion below names what was raised
        seen = e
    assert isinstance(seen, NsError) and seen.code == "NS-E125", repr(seen)
    assert "Close any program" in seen.detail and str(ctx.paths.runtime_file) in seen.detail
    assert ctx.paths.runtime_file.read_text(encoding="utf-8") == TORN and kept(ctx) == []


# ------------------------------------------------------- a fresh file is never called tuned (derived state)
class S02Fallback(Step):
    """What s02 does on the voice fallback (s02_download_bins.py): ``update_runtime``, BEFORE the tuning."""

    id = "s02_download_bins"
    title = "Download programs"

    def check(self, ctx: Any) -> bool:
        return False

    def run(self, ctx: Any) -> None:
        update_runtime(ctx, lambda rt: setattr(rt.tts, "engine", "reference"))

    def verify(self, ctx: Any) -> None:
        return None


def tuned_calls(ctx: Any) -> int:
    return len(ctx.runner.called("ns tune"))


def test_s02_fallback_writing_a_missing_file_is_followed_by_a_real_tuning(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    ctx.paths.runtime_file.unlink()     # the flag still says tuned; the file it described is gone
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=RowUi())
    assert Wizard(again, [S02Fallback(), S06Tune()], backoff=()).run() == EXIT_OK
    assert tuned_calls(again) == 1 and again.flag("tuned") == "tuned"
    assert load_runtime(again.paths).tts.engine == "reference"


def test_s02_fallback_after_a_damaged_file_is_followed_by_a_real_tuning(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    damage(ctx, TORN)
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert Wizard(again, [S02Fallback(), S06Tune()], backoff=()).run() == EXIT_OK
    assert tuned_calls(again) == 1 and again.flag("tuned") == "tuned"
    assert kept(again) == ["runtime.json.bad"]
    said = SENTENCE.format(name="runtime.json.bad")
    assert ("s02_download_bins", "warning", said) in ui.rows        # said on the row of the step that did it


def test_a_baseline_written_by_any_step_forgets_what_said_it_was_tuned(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    ctx.set_flag("smoke", "passed")
    ctx.set_flag("smoke_version", "x")
    ctx.paths.runtime_file.unlink()
    runtime_cfg.ensure_baseline(ctx)
    assert (ctx.flag("tuned"), ctx.flag("smoke"), ctx.flag("smoke_version")) == (None, None, None)


def test_a_file_with_unknown_settings_is_tuned_again(tmp_path: Path) -> None:
    """A setting's meaning may have changed within its accepted range: the numbers are not trusted."""
    ctx, _ = finished_install(tmp_path)
    newer_file(ctx)
    ctx.set_flag("smoke", "passed")
    runtime_cfg.ensure_baseline(ctx)
    assert (ctx.flag("tuned"), ctx.flag("smoke")) == (None, None)
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    newer_file(again)
    assert Wizard(again, [S06Tune()], backoff=()).run() == EXIT_OK
    assert tuned_calls(again) == 1 and again.flag("tuned") == "tuned"


def test_a_hand_edit_with_a_misspelt_name_is_worded_truthfully(tmp_path: Path) -> None:
    ctx, _ = finished_install(tmp_path)
    data = json.loads(ctx.paths.runtime_file.read_text(encoding="utf-8"))
    data["supervisor_prot"] = 8123
    ctx.paths.runtime_file.write_text(json.dumps(data), encoding="utf-8")
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert setup_again(again) == EXIT_OK
    said = ui.last()[2] or ""
    assert "supervisor_prot" in said and "hand edit" in said and "newer Nightshift, or" in said


# ------------------------------------------- every way of reading the file, through the tuning step
GARBAGE = bytes(range(0x80, 0xC0)) * 3
ROWS_DAMAGED = {
    "not utf-8 (a power cut)": GARBAGE,
    "utf-16 mark then half a character": b"\xff\xfe{",
    "an old code page, accented name": '{"profile": "gpu", "name": "caf\xe9"}'.encode("cp1252"),
    "utf-16 mark, odd garbage after it": b"\xfe\xff\x00{\x00",
    "empty": b"",
    "whitespace only": b" \r\n\t  \r\n",
    "only a NUL": b"\x00",
    "json list": b"[]",
    "json number": b"5",
    "json null": b"null",
    "json string": b'"runtime"',
    "too large to be real": b" " * (getattr(runtime_cfg, "MAX_BYTES", 1_000_000) + 1),
}


def row_context(tmp_path: Path, data: bytes) -> tuple[Any, RowUi]:
    ctx, _ = finished_install(tmp_path)
    ctx.paths.runtime_file.write_bytes(data)
    ui = RowUi()
    return make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui), ui


@pytest.mark.parametrize("data", list(ROWS_DAMAGED.values()), ids=list(ROWS_DAMAGED))
def test_row_read_damaged_is_set_aside_rewritten_and_said_through_the_tuning_step(
    tmp_path: Path, data: bytes
) -> None:
    again, ui = row_context(tmp_path, data)
    assert S06Tune().check(again) is False              # never an exception out of the step's decision
    assert setup_again(again) == EXIT_OK                # nor out of its work
    assert kept(again) == ["runtime.json.bad"]
    assert (again.paths.config / "runtime.json.bad").read_bytes() == data
    assert load_runtime(again.paths).llama.model_path == build_runtime(again).llama.model_path
    assert ui.last()[:2] == ("s06_tune", "warning") and "kept as runtime.json.bad" in (ui.last()[2] or "")
    assert tuned_calls(again) == 1 and again.flag("tuned") == "tuned"
    again2 = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=RowUi())
    assert setup_again(again2) == EXIT_OK and kept(again2) == ["runtime.json.bad"]  # again: nothing new


def recoded(text: str, encoding: str) -> bytes:
    data = json.loads(text)
    data["supervisor_port"] = 8123
    body = json.dumps(data, indent=2)
    return {"utf-8-sig": body.encode("utf-8-sig"), "utf-16": body.encode("utf-16"),
            "utf-16-be": b"\xfe\xff" + body.encode("utf-16-be")}[encoding]


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-16-be"])
def test_row_read_fine_a_byte_order_mark_or_utf_16_keeps_every_setting_as_plain_utf_8(
    tmp_path: Path, encoding: str
) -> None:
    ctx, _ = finished_install(tmp_path)
    ctx.paths.runtime_file.write_bytes(recoded(ctx.paths.runtime_file.read_text(encoding="utf-8"), encoding))
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    assert setup_again(again) == EXIT_OK
    assert kept(again) == [] and newer_kept(again) == []                    # not damaged, nothing set aside
    assert load_runtime(again.paths).supervisor_port == 8123                # every setting kept
    assert not again.paths.runtime_file.read_bytes().startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"))
    assert ui.last() == ("s06_tune", "done", None) and "no setting changed" in ui.text
    assert tuned_calls(again) == 1                  # the step ran (its check wants a plain file)


def test_row_refusal_a_folder_named_runtime_json_is_ns_e125_and_retry_works_once_it_is_gone(
    tmp_path: Path,
) -> None:
    ctx, _ = finished_install(tmp_path)
    ctx.paths.runtime_file.unlink()
    ctx.paths.runtime_file.mkdir()
    assert S06Tune().check(ctx) is False
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=RowUi())
    assert setup_again(again) == EXIT_FAILED
    row = load_install_state(again.paths).steps["s06_tune"]
    assert row.status == "failed" and (row.error or "").startswith("NS-E125")
    said = row.error or ""
    assert str(again.paths.runtime_file) in said and "is a folder, not a file" in said
    assert again.paths.runtime_file.is_dir() and kept(again) == []              # nothing moved
    again.paths.runtime_file.rmdir()
    retry = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=RowUi())
    assert setup_again(retry) == EXIT_OK and load_runtime(retry.paths).llama.model_path


def test_row_the_file_vanishing_between_the_look_and_the_read_is_a_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = finished_install(tmp_path)
    ui = RowUi()
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=ui)
    real = runtime_cfg.read_bytes_retry
    once: list[int] = []

    def vanish(path: Path) -> bytes:
        if not once:
            once.append(1)
            path.unlink()
            raise FileNotFoundError(2, "gone")
        return real(path)

    ctx.set_flag("tuned", "tuned")
    monkeypatch.setattr(runtime_cfg, "read_bytes_retry", vanish)
    assert setup_again(again) == EXIT_OK
    assert kept(again) == [] and load_runtime(again.paths).llama.model_path
    assert ui.last()[0] == "s06_tune" and ui.last()[1] in ("done", "warning")
    assert tuned_calls(again) == 1


def test_row_the_file_vanishing_between_the_read_and_the_rename_is_a_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = finished_install(tmp_path)
    damage(ctx, TORN)
    again = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, ui=RowUi())

    def gone(src: Any, dst: Any) -> None:
        Path(src).unlink()
        raise FileNotFoundError(2, "gone")

    monkeypatch.setattr(runtime_cfg, "_rename", gone)
    assert setup_again(again) == EXIT_OK
    assert kept(again) == [] and load_runtime(again.paths).llama.model_path


# ============================================================================================================
# One mechanism: the state of the existing file x what the answers changed, through the wizard's real steps
# ============================================================================================================
# The questions step (s01) records what the answers changed, the models step (s03) fetches the model, the
# tuning step (s06) settles the file, the settings step (s07) completes it. The invariant every cell is held
# to: the final runtime.json is what a fresh install with these answers writes, plus exactly the settings of
# the existing file that setup keeps by design (``expected``), and no state of the old file makes an answer
# be ignored.
FILE_ROWS = ("ok", "bom", "utf16", "newer", "missing", "damaged")
ANSWER_ROWS = ("unchanged", "tier", "dev_to_real", "real_to_dev", "engine")
WHOLE = (
    "config\\runtime.json is built again from your answers (it was made for another kind of install). "
    "The file as it was is kept as runtime.json.other. Settings that were only in it are not carried over: "
    "a port or an engine you changed by hand or in Settings, and the tuned numbers. Set them again in "
    "Settings, or copy them from that file."
)


def real_steps() -> list[Step]:
    from installer.stage2.steps.s01_questions import S01Questions
    from installer.stage2.steps.s03_download_models import S03DownloadModels
    from installer.stage2.steps.s07_configs import S07Configs

    return [S01Questions(), S03DownloadModels(), S06Tune(), S07Configs()]


def answers_of(change: str, *, second: bool) -> tuple[dict[str, Any], bool]:
    """(the answers, dev install or not) of the first install and of the setup run over it."""
    first = example_answers(llm_tier="balanced")
    if not second:
        return first, change == "dev_to_real"
    if change == "tier":
        return example_answers(llm_tier="best"), False
    if change == "engine":
        return example_answers(llm_tier="balanced", fake_voice=True, fake_footage=True), False
    return first, change == "real_to_dev"


def run_setup(
    tmp_path: Path, answers: dict[str, Any], dev: bool = False, steps: list[Step] | None = None
) -> tuple[Any, RowUi, int]:
    """One run of setup with an answers file next to INSTALL.cmd, through the real wizard and real steps."""
    src = tmp_path / "answers-in-use.json"
    src.write_text(json.dumps(answers), encoding="utf-8")
    ui = RowUi(interactive=False)
    ctx = make_test_context(tmp_path, raw_answers=answers, ui=ui, dev_mode=dev)
    ctx.answers_path = src
    return ctx, ui, Wizard(ctx, steps or real_steps(), backoff=()).run()


def owner_settings(ctx: Any) -> dict[str, Any]:
    """What the owner changed since setup (two ports) and what tuning found (33 layers). Returns the file's
    settings as this version reads them."""
    rt = load_runtime(ctx.paths)
    rt.supervisor_port, rt.llama.port, rt.llama.flags.n_gpu_layers = 8123, 8085, 33
    save_runtime(ctx.paths, rt)
    return rt.model_dump()


def put_state(ctx: Any, state: str) -> bytes | None:
    """Put the file in one of the states of the table. Returns the bytes left on disk (None: no file)."""
    path = ctx.paths.runtime_file
    text = path.read_text(encoding="utf-8")
    if state == "missing":
        path.unlink()
        return None
    if state == "newer":
        data = json.loads(text)
        data["future_setting"] = {"a": 1}
        data["llama"]["flags"]["future_flag"] = 3
        text = json.dumps(data, indent=2)
    raw = {
        "ok": text.encode("utf-8"), "newer": text.encode("utf-8"), "bom": text.encode("utf-8-sig"),
        "utf16": text.encode("utf-16"), "damaged": TORN.encode("utf-8"),
    }[state]
    path.write_bytes(raw)
    return raw


def expected(ctx: Any, existing: dict[str, Any] | None, change: str) -> dict[str, Any]:
    """The invariant, written out without the code under test: a fresh install's file plus the kept settings.

    Kept by design (installer/stage2/reinstall.py): every setting when the answers did not change; all but
    the language model and its flags when the tier changed; all but the three engine-test settings when those
    answers changed; none when the file was made for another kind of install, is missing or is damaged."""
    fresh = build_runtime(ctx).model_dump()
    if existing is None or change in ("dev_to_real", "real_to_dev"):
        return fresh
    out = json.loads(json.dumps(existing))
    if change == "tier":
        for name in ("model_path", "flags"):
            out["llama"][name] = fresh["llama"][name]
    if change == "engine":
        out["fake_footage"], out["tiny_story"] = fresh["fake_footage"], fresh["tiny_story"]
        out["tts"]["engine"] = fresh["tts"]["engine"]
    return out


def first_install(tmp_path: Path, change: str) -> tuple[Any, dict[str, Any]]:
    answers, dev = answers_of(change, second=False)
    ctx, _ui, code = run_setup(tmp_path, answers, dev)
    assert code == EXIT_OK and load_runtime(ctx.paths).dev_mode is dev
    return ctx, owner_settings(ctx)


def aside(ctx: Any) -> list[str]:
    return sorted(p.name for p in ctx.paths.config.glob("runtime.json.*"))


def row_of(ui: RowUi, step: str) -> tuple[str, str, str | None]:
    return [r for r in ui.rows if r[0] == step][-1]


def plain_and_final(ctx: Any, want: dict[str, Any]) -> None:
    raw = ctx.paths.runtime_file.read_bytes()
    assert not raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"))    # what the engine reads: plain
    assert load_runtime(ctx.paths).model_dump() == want
    assert ctx.flag("runtime_pending") is None                               # nothing is owed any more


@pytest.mark.parametrize("change", ANSWER_ROWS)
@pytest.mark.parametrize("state", FILE_ROWS)
def test_cell_a_finished_install_run_again_ends_on_the_answers_and_the_kept_settings(
    tmp_path: Path, state: str, change: str
) -> None:
    """A finished install (its flags say tuned), the file put in ``state``, setup run with ``change``."""
    ctx, settings = first_install(tmp_path, change)
    raw = put_state(ctx, state)
    has_settings = state in ("ok", "bom", "utf16", "newer")
    answers, dev = answers_of(change, second=True)
    again, ui, code = run_setup(tmp_path, answers, dev)
    assert code == EXIT_OK
    # 1. the final file
    plain_and_final(again, expected(again, settings if has_settings else None, change))
    assert load_runtime(again.paths).dev_mode is dev
    # 2. what is kept aside: the original of a file that lost something, never anything else
    want_aside = {"newer": ["runtime.json.newer"], "damaged": ["runtime.json.bad"]}.get(state, [])
    if has_settings and state != "newer" and change in ("dev_to_real", "real_to_dev"):
        want_aside = ["runtime.json.other"]     # a readable file of another kind is rebuilt: kept too
    assert aside(again) == want_aside
    for name in want_aside:
        assert (again.paths.config / name).read_bytes() == raw
    # 3. whether tuning runs again
    untouched = state == "ok" and change == "unchanged"
    if dev:
        assert tuned_calls(again) == 0 and again.flag("tuned") == "defaults"
        assert "nothing to tune" in ui.text
    else:
        assert tuned_calls(again) == (0 if untouched else 1) and again.flag("tuned") == "tuned"
    # 4. what the owner reads, and where
    step, status, detail = row_of(ui, "s06_tune")
    detail = detail or ""
    kind = change in ("dev_to_real", "real_to_dev")
    if untouched:
        assert (status, detail) == ("done", "already complete")
    elif state == "damaged":
        assert status == "warning" and detail == SENTENCE.format(name="runtime.json.bad")
    elif state == "missing":
        assert (status, detail) == ("done", "")
    elif state == "newer":
        assert status == "warning" and "future_setting" in detail and "llama.flags.future_flag" in detail
        assert "kept as runtime.json.newer" in detail and "Tuning runs again" in detail
        if kind:
            assert "made for another kind of install" in detail
            assert "no setting of the old file is carried over" in detail and "was kept" not in detail
        elif change == "tier":
            assert "was kept, except what your answers changed (the language model)" in detail
        elif change == "engine":
            assert "was kept, except what your answers changed (the engine test)" in detail
        else:
            assert "they were removed and every other setting was kept." in detail
    elif kind:
        assert (status, detail) == ("warning", WHOLE) and WHOLE in ui.text
    else:
        assert (status, detail) == ("done", "")
    if has_settings and not kind and change == "tier":
        assert "Language model: config\\runtime.json now uses" in ui.text
    if has_settings and not kind and change == "engine":
        assert "Engine test: config\\runtime.json now follows" in ui.text
    if state in ("bom", "utf16"):
        was = "UTF-16" if state == "utf16" else "UTF-8 with a byte order mark"
        same = " (no setting changed)" if change == "unchanged" else ""
        assert f"runtime.json was {was}; it is written again as plain UTF-8{same}." in ui.text
        if change != "unchanged":
            assert "no setting changed" not in ui.text
    assert report(again)["steps"]["s06_tune"].get("note") == (detail if status == "warning" else None)
    # 5. a third run finds nothing to do and says nothing
    third, ui3, code3 = run_setup(tmp_path, answers, dev)
    assert code3 == EXIT_OK and tuned_calls(third) == 0 and aside(third) == want_aside
    assert row_of(ui3, "s06_tune")[1:] == ("done", "already complete")
    assert third.paths.runtime_file.read_bytes() == again.paths.runtime_file.read_bytes()


# --------------------------------------------------------------------------- unreadable right now x answers
@pytest.mark.parametrize("change", ANSWER_ROWS)
@pytest.mark.parametrize("how", ["held", "folder"])
def test_cell_a_file_that_cannot_be_read_stops_setup_by_name_and_no_answer_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str, change: str
) -> None:
    ctx, settings = first_install(tmp_path, change)
    path = ctx.paths.runtime_file
    before = path.read_bytes()
    saved = (ctx.paths.config / "answers.json").read_bytes()

    def held(p: Any) -> bytes:
        raise PermissionError(13, "The process cannot access the file because it is being used")

    if how == "folder":
        path.unlink()
        path.mkdir()
    answers, dev = answers_of(change, second=True)
    with monkeypatch.context() as m:
        if how == "held":
            m.setattr(runtime_cfg, "read_bytes_retry", held)
        again, ui, code = run_setup(tmp_path, answers, dev)
        assert code == EXIT_FAILED
        failed = {k: v for k, v in load_install_state(again.paths).steps.items() if v.status == "failed"}
        assert list(failed) in (["s01_questions"], ["s06_tune"]), failed
        error = next(iter(failed.values())).error or ""
        assert error.startswith("NS-E125") and str(path) in error and "Retry" in error
        assert ("is a folder, not a file" if how == "folder" else "Close any program") in error
        assert report(again)["last_error"]["code"] == "NS-E125"
        # nothing was changed, nothing was guessed, and the answers that would change it were not saved
        assert path.is_dir() if how == "folder" else path.read_bytes() == before
        assert aside(again) == []
        if change in ("tier", "engine", "dev_to_real"):         # the questions step had something to apply
            assert list(failed) == ["s01_questions"]
            assert (again.paths.config / "answers.json").read_bytes() == saved
            assert again.flag("runtime_pending") is None
    # the owner closed the program (or removed the folder) and ran setup again
    if how == "folder":
        path.rmdir()
    retry, _ui, code = run_setup(tmp_path, answers, dev)
    assert code == EXIT_OK
    plain_and_final(retry, expected(retry, settings if how == "held" else None, change))
    rebuilt = how == "held" and change in ("dev_to_real", "real_to_dev")
    assert load_runtime(retry.paths).dev_mode is dev
    assert aside(retry) == (["runtime.json.other"] if rebuilt else [])
    if rebuilt:
        assert (retry.paths.config / "runtime.json.other").read_bytes() == before


# ------------------------------------------------------------------------------ vanishing mid-way x answers
class Vanish(Step):
    """The file goes away after the questions step looked at it and before any other step does."""

    id = "v_vanish"
    title = "The file is deleted"

    def check(self, ctx: Any) -> bool:
        return False

    def run(self, ctx: Any) -> None:
        ctx.paths.runtime_file.unlink(missing_ok=True)

    def verify(self, ctx: Any) -> None:
        return None


@pytest.mark.parametrize("change", ANSWER_ROWS)
def test_cell_a_file_that_vanishes_after_the_questions_is_a_fresh_file_of_the_answers(
    tmp_path: Path, change: str
) -> None:
    ctx, _settings = first_install(tmp_path, change)
    put_state(ctx, "bom")
    answers, dev = answers_of(change, second=True)
    s01, s03, s06, s07 = real_steps()
    again, ui, code = run_setup(tmp_path, answers, dev, [s01, Vanish(), s03, s06, s07])
    assert code == EXIT_OK
    plain_and_final(again, expected(again, None, change))   # what was owed to the old file is not owed now
    assert aside(again) == [] and row_of(ui, "s06_tune")[1:] == ("done", None)
    assert "kept as" not in ui.text and "is built again from your answers (it was made" not in ui.text


# ----------------------------------------------------------------- killed in the middle of the one rewrite
@pytest.mark.parametrize("state", ["ok", "bom", "newer"])
def test_killed_while_the_file_is_written_the_rerun_still_ends_on_the_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    ctx, settings = first_install(tmp_path, "tier")
    raw = put_state(ctx, state)
    answers, dev = answers_of("tier", second=True)

    def die(paths: Any, rt: Any) -> None:
        raise Killed()

    with monkeypatch.context() as m:
        m.setattr(runtime_cfg, "save_runtime", die)
        with pytest.raises(Killed):
            run_setup(tmp_path, answers, dev)
    assert ctx.paths.runtime_file.read_bytes() == raw           # the old file, whole
    assert ctx.flag("tuned") is None and ctx.flag("runtime_pending") == {"llama": True}     # still owed
    again, _ui, code = run_setup(tmp_path, answers, dev)
    assert code == EXIT_OK and tuned_calls(again) == 1
    plain_and_final(again, expected(again, settings, "tier"))
    # the original of a newer file is kept once per attempt, never overwritten
    assert aside(again) == (["runtime.json.newer", "runtime.json.newer.1"] if state == "newer" else [])
    for name in aside(again):
        assert (again.paths.config / name).read_bytes() == raw


# ---------------------------------------------------- the route of the review: "uninstall, keep my data"
def uninstalled_keeping_data(ctx: Any) -> None:
    """What the uninstaller leaves of config: the answers, runtime.json, the channel. The install's state
    and its flags are removed, so the next setup is a new install over kept data."""
    for name in ("install_state.json", "wizard_flags.json"):
        (ctx.paths.config / name).unlink()


@pytest.mark.parametrize(
    ("state", "change"),
    [("bom", "dev_to_real"), ("utf16", "dev_to_real"), ("newer", "dev_to_real"), ("ok", "dev_to_real"),
     ("bom", "real_to_dev"), ("newer", "tier"), ("bom", "tier"), ("utf16", "engine"), ("newer", "engine")],
)
def test_kept_data_then_install_again_no_state_of_the_kept_file_hides_an_answer(
    tmp_path: Path, state: str, change: str
) -> None:
    """The reviewer's case first: a dev install whose runtime.json has a byte order mark (PowerShell 5.1's
    ``Set-Content -Encoding UTF8`` writes one), uninstalled keeping data, installed for real."""
    ctx, settings = first_install(tmp_path, change)
    raw = put_state(ctx, state)
    uninstalled_keeping_data(ctx)
    answers, dev = answers_of(change, second=True)
    again, ui, code = run_setup(tmp_path, answers, dev)
    assert code == EXIT_OK
    final = load_runtime(again.paths)
    assert final.dev_mode is dev                                # a real install never runs the fake engines
    plain_and_final(again, expected(again, settings, change))
    if change in ("dev_to_real", "real_to_dev"):
        assert "was made for another kind of install" in ui.text               # said by the questions step
        detail = row_of(ui, "s06_tune")[2] or ""
        assert row_of(ui, "s06_tune")[1] == "warning" and "made for another kind of install" in detail
        name = "runtime.json.newer" if state == "newer" else "runtime.json.other"
        assert aside(again) == [name] and f"kept as {name}" in detail      # the old file, never deleted
        assert (again.paths.config / name).read_bytes() == raw
    if change == "tier":
        assert Path(final.llama.model_path).is_file() and final.llama.flags.n_gpu_layers != 33
        assert "Language model: your answers name" in ui.text                   # said by the questions step
    if change == "engine":
        assert final.tts.engine == "fake" and final.fake_footage is True


@pytest.mark.parametrize(
    ("state", "said"),
    [("ok", "Kept config\\runtime.json (ports, tuning, engines)."),
     ("bom", "Kept config\\runtime.json (ports, engines). It was saved as UTF-8 with a byte order mark, so "
             "setup writes it again as plain UTF-8 with the same settings.\n"),
     ("newer", "Kept config\\runtime.json (ports, engines), except settings this version does not know "
               "(llama.flags.future_flag, future_setting): setup removes them, keeps the file as it was next "
               "to it, and tuning runs again.")],
)
def test_kept_data_and_the_same_answers_the_questions_step_says_what_is_kept_truthfully(
    tmp_path: Path, state: str, said: str
) -> None:
    ctx, settings = first_install(tmp_path, "unchanged")
    put_state(ctx, state)
    uninstalled_keeping_data(ctx)
    answers, dev = answers_of("unchanged", second=True)
    again, ui, code = run_setup(tmp_path, answers, dev)
    assert code == EXIT_OK and said in ui.text + "\n"
    plain_and_final(again, expected(again, settings, "unchanged"))
    assert tuned_calls(again) == 1      # a new install tunes


def test_a_file_another_step_wrote_plain_is_not_tuned_again_and_nothing_said_it_would_be(
    tmp_path: Path,
) -> None:
    """Whether tuning runs again for a file with a byte order mark depends on which step rewrites it: when
    s02 (the voice fallback) or s03 does, the tuning step finds a settled file. So the questions step's line
    promises the rewrite only, and no line of the run speaks of tuning again."""
    from installer.stage2.steps.s01_questions import S01Questions

    ctx, settings = first_install(tmp_path, "unchanged")
    put_state(ctx, "bom")
    answers, dev = answers_of("unchanged", second=True)
    again, ui, code = run_setup(tmp_path, answers, dev, [S01Questions(), S02Fallback(), S06Tune()])
    assert code == EXIT_OK and tuned_calls(again) == 0
    assert row_of(ui, "s06_tune")[1:] == ("done", "already complete")
    assert "written again as plain UTF-8 (no setting changed)" in ui.text
    assert "uning runs again" not in ui.text
    want = {**settings, "tts": {**settings["tts"], "engine": "reference"}}
    assert load_runtime(again.paths).model_dump() == want


def test_the_whole_setup_dev_install_with_a_byte_order_mark_made_real_by_install_cmd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same case end to end: ``main()`` with every step of the wizard, the real uninstall script between
    the two installs, and the byte order mark written the way an owner's PowerShell 5.1 writes it."""
    from installer.stage2.wizard import main
    from tests.test_install_done_means_done import uninstall_with_the_script
    from tests.test_wizard_e2e import args, write_answers

    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)
    monkeypatch.delenv("NIGHTSHIFT_DEV_MODE", raising=False)
    answers = write_answers(tmp_path)
    home = tmp_path / "home"
    runtime = home / "config" / "runtime.json"
    assert main(args(tmp_path, answers, "--dev")) == 0
    assert json.loads(runtime.read_text(encoding="utf-8"))["dev_mode"] is True
    uninstall_with_the_script(home)                             # "keep my data": config and models stay
    runtime.write_bytes(runtime.read_text(encoding="utf-8").encode("utf-8-sig"))
    assert not (home / "config" / "install_state.json").exists()
    assert runtime.read_bytes()[:3] == b"\xef\xbb\xbf"

    assert main(args(tmp_path, answers)) == 0
    final = json.loads(runtime.read_text(encoding="utf-8"))     # plain UTF-8: this read is the engine's
    assert final["dev_mode"] is False and final["profile"] == "gpu"
    model = Path(final["llama"]["model_path"])
    assert model.is_file() and not (model.parent / (model.name + ".dev-stub")).exists()
    state = load_install_state(Paths(home))
    assert state.dev_mode is False and state.completed_at
    assert state.steps["s06_tune"].status == "done" and WHOLE in (state.steps["s06_tune"].error or "")


# ---------------------------------------------- the other readers of the file go through the same question
def test_the_settings_step_alone_does_not_call_a_file_done_that_the_engine_cannot_read(
    tmp_path: Path,
) -> None:
    """s07's check asked only whether the file exists, so a run of it called a file with a byte order mark
    done and left it. It asks the one question now, and its work writes the same settings plain."""
    from installer.stage2.steps.s07_configs import S07Configs

    ctx, settings = first_install(tmp_path, "unchanged")
    put_state(ctx, "bom")
    answers, dev = answers_of("unchanged", second=True)
    again, _ui, code = run_setup(tmp_path, answers, dev, [S07Configs()])
    assert code == EXIT_OK
    assert not again.paths.runtime_file.read_bytes().startswith(b"\xef\xbb\xbf")
    assert load_runtime(again.paths).model_dump() == expected(again, settings, "unchanged")


def test_switching_the_mode_of_an_install_does_not_stop_on_a_file_it_cannot_delete(tmp_path: Path) -> None:
    """``reset_for_mode_switch`` (before the wizard starts) deletes runtime.json when an install changes
    between dev and real. A folder of that name (or a held file) made it raise before any step ran; now the
    tuning step refuses by name, and once the name is free the install ends as the new kind."""
    from installer.stage2.devmode import reset_for_mode_switch

    ctx, _settings = first_install(tmp_path, "dev_to_real")
    path = ctx.paths.runtime_file
    path.unlink()
    path.mkdir()
    reset: Any = None
    try:
        reset = reset_for_mode_switch(ctx.paths.home, False)
    except OSError as e:
        reset = e
    assert reset is True, repr(reset)
    assert not (ctx.paths.config / "wizard_flags.json").exists() and path.is_dir()
    assert aside(ctx) == []                                     # a folder is never moved
    answers, dev = answers_of("dev_to_real", second=True)
    again, _ui, code = run_setup(tmp_path, answers, dev)
    assert code == EXIT_FAILED
    steps = load_install_state(again.paths).steps
    failed = [v.error or "" for v in steps.values() if v.status == "failed"]
    assert len(failed) == 1 and failed[0].startswith("NS-E125") and "is a folder, not a file" in failed[0]
    path.rmdir()
    retry, _ui, code = run_setup(tmp_path, answers, dev)
    assert code == EXIT_OK
    plain_and_final(retry, expected(retry, None, "dev_to_real"))


def test_a_file_of_the_other_kind_that_survived_is_built_again_by_the_tuning_step_itself(
    tmp_path: Path,
) -> None:
    """The questions step has nothing to apply (a dev install sees the same answers), so it never looks at
    the file: the tuning step compares the kind of install on every run."""
    ctx, _settings = first_install(tmp_path, "real_to_dev")
    assert S06Tune().check(ctx) is True
    answers, dev = answers_of("real_to_dev", second=True)
    again, ui, code = run_setup(tmp_path, answers, dev)
    assert code == EXIT_OK
    assert "s01_questions" not in [r[0] for r in ui.rows if r[1] == "running"]      # s01 did not run
    assert load_runtime(again.paths).dev_mode is True and row_of(ui, "s06_tune")[1:] == ("warning", WHOLE)


def test_a_model_that_never_arrived_is_said_on_the_row_and_stays_owed(tmp_path: Path) -> None:
    """The other end of "the answers win": when the model the answers name is not on disk, the settings keep
    the model they have (the nightly run must find a file), and the owner reads that on the row."""
    from installer.stage2.steps.s01_questions import S01Questions

    ctx, settings = first_install(tmp_path, "tier")
    put_state(ctx, "bom")
    answers, dev = answers_of("tier", second=True)
    again, ui, code = run_setup(tmp_path, answers, dev, [S01Questions(), S06Tune()])     # no models step
    assert code == EXIT_OK
    assert load_runtime(again.paths).model_dump() == settings           # as it was, plain UTF-8
    status, detail = row_of(ui, "s06_tune")[1:]
    assert status == "warning" and "is not in models\\llm yet" in (detail or "")
    assert "keeps using" in (detail or "")
    assert again.flag("runtime_pending") == {"llama": True} and S06Tune().check(again) is False
    done, _ui, code = run_setup(tmp_path, answers, dev)                   # the models step runs this time
    assert code == EXIT_OK
    plain_and_final(done, expected(done, settings, "tier"))


def test_switching_the_mode_keeps_the_old_file_as_other_and_never_over_an_earlier_one(tmp_path: Path) -> None:
    """A normal mode switch (the state file says the other mode): the old runtime.json was deleted. It is
    kept as runtime.json.other now, and a second switch keeps its file beside the first."""
    from installer.stage2.devmode import reset_for_mode_switch

    ctx, _settings = first_install(tmp_path, "dev_to_real")
    dev_bytes = ctx.paths.runtime_file.read_bytes()
    assert reset_for_mode_switch(ctx.paths.home, False) is True
    assert aside(ctx) == ["runtime.json.other"]         # the base deleted the file: nothing was kept
    assert (ctx.paths.config / "runtime.json.other").read_bytes() == dev_bytes
    assert not ctx.paths.runtime_file.exists()
    answers, dev = answers_of("dev_to_real", second=True)
    real, _ui, code = run_setup(tmp_path, answers, dev)
    assert code == EXIT_OK and load_runtime(real.paths).dev_mode is False
    real_bytes = real.paths.runtime_file.read_bytes()
    assert reset_for_mode_switch(real.paths.home, True) is True
    assert aside(real) == ["runtime.json.other", "runtime.json.other.1"]
    assert (real.paths.config / "runtime.json.other").read_bytes() == dev_bytes
    assert (real.paths.config / "runtime.json.other.1").read_bytes() == real_bytes
