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
from installer.stage2.base import Step
from installer.stage2.runtime_cfg import build_runtime, update_runtime
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
NOT_THE_MODEL = '{"profile": "gpu", "supervisor_port": "not a port"}'     # JSON, a known field, bad value


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
