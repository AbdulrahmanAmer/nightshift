"""Card 206-carried: the loose ends of the update rollback (the review of pull request 206, F-10c).

Entry is where the product enters: `ns update` (`update.update_app`), `ns update --rollback`
(`update.rollback_app`), a start of the program (`first_start.ensure`, which every settings writer calls) and
`ns doctor` (`doctor.diagnose`). The install is the fake one of tests/test_first_start_snapshot.py: a
temporary home, fake program folders, 0.1.14's frozen models as "the older version". No engine, no network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from installer.stage2.web import appcopy
from ns_common import config_snapshot, first_start, rollback_intent
from ns_common.errors import NsError
from ns_supervisor import doctor, update
from ns_supervisor.client import SupervisorClient
from tests.test_first_start_snapshot import (
    RECORD,
    Install,
    Stop,
    _stop_at_rename,
    first_start_writes,
    older_version_loads_everything,
    owner_uses_the_new_version,
    read_config,
    seed,
)
from tests.test_ns_update import Env, make_zip

LEFT = re.compile(r"^app\.swap-left-\d{8}-\d{4}(-\d+)?$")
SWAP_EVENT = "rollback_swap_folder"


@pytest.fixture(autouse=True)
def a_new_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(update, "RENAME_PAUSE_S", 0.01)
    first_start.reset()
    yield
    first_start.reset()


@pytest.fixture
def inst(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Install:
    """0.1.14 updated to 0.1.15 by the older updater; 0.1.15 started (the first-start copy is taken) and the
    owner used a setting 0.1.14 does not have."""
    it = Install(tmp_path, monkeypatch)
    seed(it.paths)
    it.old_updater("0.1.15")
    it.start()
    first_start_writes(it.paths)
    owner_uses_the_new_version(it.paths)
    return it


def folders(inst: Install) -> list[str]:
    """Every program folder (and what is named like one) in the home."""
    return sorted(p.name for p in inst.home.iterdir() if p.name.startswith("app") and p.is_dir())


def version_in(folder: Path) -> str:
    return (folder / "VERSION").read_text(encoding="utf-8").strip()


def lines_of(inst: Install, event: str) -> list[dict[str, Any]]:
    """What the starts of the program logged as `event` (logs/first_start-<date>.jsonl)."""
    out: list[dict[str, Any]] = []
    for f in sorted(inst.paths.logs.glob("first_start-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line)["event"] == event:
                out.append(json.loads(line))
    return out


def swap_lines(inst: Install) -> list[dict[str, Any]]:
    """What a start said about a swap folder."""
    return lines_of(inst, SWAP_EVENT)


def files_in(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def doctor_row(inst: Install, on_wait: Any = None) -> tuple[bool, str]:
    """The "program folders" row of `ns doctor` (run by the program in <home>/app)."""

    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no engine")

    client = SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(refuse)))
    rows = {name: (ok, detail) for name, ok, detail in doctor.diagnose(inst.paths, client, on_wait)}
    row = rows.get("program folders")
    assert row is not None, f"ns doctor has no row for the program folders: {sorted(rows)}"
    return row


def start(inst: Install) -> None:
    """A new process of the program in <home>/app that writes a settings file."""
    inst.start()
    first_start.ensure(inst.paths.config)


# ------------------------------------------------------------------- 1: a stale record of a failed restore
def _failed_restore_then_put_in_place_again(inst: Install, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rollback to 0.1.14 whose restore failed and that stopped right after the swap (0.1.14 has no code
    to finish it); then 0.1.14 was put in place again (the same build, a new completion marker)."""

    def cannot(*a: object, **k: object) -> object:
        raise config_snapshot.SnapshotError("could not restore runtime.json: in use by another program")

    with monkeypatch.context() as m:
        m.setattr(config_snapshot, "restore", cannot)
        _stop_at_rename(3, after=True)(inst, m)
        with pytest.raises(Stop):
            inst.rollback()
    record = json.loads((inst.home / RECORD).read_text(encoding="utf-8"))
    assert record["stage"] == "prepared" and record["after_swap"]["mark_unrestored"] is True
    assert inst.version_now() == "0.1.14"
    snap = config_snapshot.read(inst.copy)
    assert snap.fingerprint == record["to"]["fingerprint"] == config_snapshot.program_fingerprint(inst.app)
    marker = inst.app / appcopy.MARKER
    marker.write_text(appcopy.fingerprint(inst.app), encoding="utf-8")
    later = marker.stat().st_mtime_ns + 5_000_000_000
    os.utime(marker, ns=(later, later))
    assert config_snapshot.install_stamp(inst.app) != record["to"]["stamp"]
    assert not config_snapshot.is_unrestored(inst.copy)  # the rollback never got to say so


@pytest.mark.parametrize("how", ["a start", "ns update", "killed between the mark and the record"])
def test_a_stale_record_of_a_failed_restore_marks_the_copy_unrestored(
    inst: Install, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str
) -> None:
    """Box 1: the record is stale (another install of the program), and it is the only thing that knows the
    copy for 0.1.14 was never put back: dropping it let the next update save the settings files of 0.1.15
    over that copy under 0.1.14's name."""
    _failed_restore_then_put_in_place_again(inst, monkeypatch)
    copy_before = inst.copy.read_bytes()

    if how == "killed between the mark and the record":

        def killed(app: Path) -> None:
            raise Stop

        with monkeypatch.context() as m:
            m.setattr(rollback_intent, "clear", killed)
            inst.start()
            with pytest.raises(Stop):
                first_start.ensure(inst.paths.config)
        assert (inst.home / RECORD).is_file()  # stale again at the next start
    if how != "ns update":
        start(inst)
        said = [r["msg"] for r in lines_of(inst, "rollback_recovered")]
        assert said and "does not belong to the programs installed now" in said[-1]
        assert "the next update keeps it" in said[-1], said[-1]
        assert config_snapshot.is_unrestored(inst.copy)
        assert not (inst.home / RECORD).exists()

    res = update.update_app(make_zip(tmp_path, "0.1.16"), inst.app, Env().env(), paths=inst.paths)

    assert not (inst.home / RECORD).exists()
    assert config_snapshot.is_unrestored(inst.copy)  # still the copy 0.1.14 wrote, and marked
    assert inst.copy.read_bytes() == copy_before
    kept = [n for n in res.notes if "is kept in" in n]
    assert len(kept) == 1 and "could not put it back" in kept[0], res.notes
    if how == "ns update":  # the update itself met the record, and its result says so
        assert any("does not belong to the programs installed now" in n for n in res.notes), res.notes


def test_a_copy_that_cannot_be_marked_keeps_the_record_and_the_update_refuses(
    inst: Install, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fail-closed side of box 1: while the mark cannot be written the record stays (it is the only
    thing that knows), every start tries again, and an update refuses with the reason."""
    _failed_restore_then_put_in_place_again(inst, monkeypatch)

    def cannot(path: Path) -> None:
        raise config_snapshot.SnapshotError(f"could not record that {path.name} was not put back: disk full")

    with monkeypatch.context() as m:
        m.setattr(config_snapshot, "mark_unrestored", cannot)
        with pytest.raises(NsError) as ei:
            update.update_app(make_zip(tmp_path, "0.1.16"), inst.app, Env().env(), paths=inst.paths)
    assert ei.value.code == "NS-E702" and "disk full" in ei.value.detail
    assert "Nothing was changed" in ei.value.detail and "try again" in ei.value.detail
    assert (inst.home / RECORD).is_file() and inst.version_now() == "0.1.14"

    start(inst)  # the disk has room again
    assert config_snapshot.is_unrestored(inst.copy) and not (inst.home / RECORD).exists()


@pytest.mark.parametrize("zip_now", ["the copy of another program", "none"])
def test_a_stale_record_marks_only_the_copy_of_the_program_it_went_back_to(
    inst: Install, monkeypatch: pytest.MonkeyPatch, zip_now: str
) -> None:
    """The mark says "this copy was not put back": it is never put on a copy the record does not speak of."""
    _failed_restore_then_put_in_place_again(inst, monkeypatch)
    inst.copy.unlink()
    if zip_now != "none":
        config_snapshot.save(inst.copy, {}, version="0.1.9", fingerprint="0.1.9|a|b", taken_by="update")
    start(inst)
    said = [r["msg"] for r in lines_of(inst, "rollback_recovered")]
    assert said and "does not belong to the programs installed now" in said[-1]
    assert "the next update keeps it" not in said[-1]
    assert not (inst.home / RECORD).exists() and not config_snapshot.is_unrestored(inst.copy)
    assert not inst.copy.with_name(inst.copy.name + config_snapshot.UNRESTORED_SUFFIX).exists()


# ------------------------------------------------------------------------------ 2: a leftover swap folder
def _no_app(inst: Install) -> str:
    """A power loss after the first rename: the leaving program is in app.swap, nothing in app."""
    inst.app.rename(inst.home / "app.swap")
    return "no app"


def _no_prev(inst: Install) -> str:
    """After the second rename with no code to finish it: the program backup is in app.swap."""
    (inst.home / "app.prev").rename(inst.home / "app.swap")
    return "no prev"


def _all_three(inst: Install) -> str:
    """app, app.prev and a third program folder in app.swap."""
    shutil.copytree(inst.home / "app.prev", inst.home / "app.swap")
    return "all three"


ROWS = {"no app": _no_app, "no prev": _no_prev, "all three": _all_three}
SAID = {
    "no app": "The program folder was put back from app.swap (an interrupted rollback).",
    "no prev": "app.swap was renamed to app.prev: ns update --rollback goes to 0.1.14 again.",
    "all three": "is a program folder left by an interrupted rollback; Nightshift does not use it. Delete it "
    "when you no longer need it.",
}


@pytest.mark.parametrize("route", ["a start", "ns update", "ns update --rollback"])
@pytest.mark.parametrize("row", sorted(ROWS))
def test_a_leftover_swap_folder_is_settled_and_said(
    inst: Install, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, row: str, route: str
) -> None:
    """Box 2: `app.swap` is never left where nothing looks for it, never deleted, and the owner is told."""
    ROWS[row](inst)
    assert not (inst.home / RECORD).exists()
    files_in_swap = files_in(inst.home / "app.swap")
    expect = SAID[row]
    swap_version = version_in(inst.home / "app.swap")

    if route == "a start":
        with monkeypatch.context() as m:  # killed just before the rename: nothing has moved
            real = os.rename

            def killed(src: Any, dst: Any, **k: Any) -> None:
                if Path(src).name == "app.swap":
                    raise Stop
                real(src, dst, **k)

            m.setattr(os, "rename", killed)
            inst.start()
            try:
                first_start.ensure(inst.paths.config)
            except Stop:
                pass
        assert (inst.home / "app.swap").is_dir(), "the base does not look at app.swap at all"
        start(inst)  # run again
        lines = swap_lines(inst)
        assert len(lines) == 1 and lines[0]["level"] == "WARN", lines
        said = [lines[0]["msg"]]
    elif route == "ns update":
        res = update.update_app(make_zip(tmp_path, "0.1.16"), inst.app, Env().env(), paths=inst.paths)
        said = res.notes
        assert inst.version_now() == "0.1.16" and version_in(inst.home / "app.prev") == "0.1.15"
        if row == "no prev":  # the update replaces the backup next: no sentence promises a way back to it
            expect = "app.swap was renamed to app.prev (an interrupted rollback left it)."
            assert not any("goes to 0.1.14 again" in n for n in said), said
            assert any("the earlier program backup (0.1.14)" in n for n in said), said
    elif row == "no prev":
        # the state an interrupted rollback leaves after its second rename (0.1.14's writes no record): this
        # run finishes that rollback and stops; it does not swap straight back
        e = Env()
        res = update.rollback_app(inst.app, e.env(), paths=inst.paths)
        said = res.notes
        assert inst.version_now() == "0.1.15" and version_in(inst.home / "app.prev") == "0.1.14"
        assert "It was finished now, and nothing else was done: 0.1.15 is the program" in res.settings
        assert "again if you want to go to 0.1.14" in res.settings and "sync" in e.calls
        assert (res.old_version, res.new_version) == ("0.1.15", "0.1.15")
        again = update.rollback_app(inst.app, Env().env(), paths=inst.paths)  # run again: now it goes back
        assert inst.version_now() == "0.1.14" and "finished now" not in again.settings
        older_version_loads_everything(inst.paths)
        inst.rollback()  # (and forward, so the checks below meet the same folders in every row)
    else:
        res = update.rollback_app(inst.app, Env().env(), paths=inst.paths)
        said = res.notes
        assert inst.version_now() == "0.1.14" and version_in(inst.home / "app.prev") == "0.1.15"
        older_version_loads_everything(inst.paths)

    assert not (inst.home / "app.swap").exists()
    assert any(expect in s for s in said), said
    left = [n for n in folders(inst) if n not in ("app", "app.prev")]
    if row == "all three":  # set aside under a name the owner is told, with everything in it
        assert len(left) == 1 and LEFT.match(left[0]), left
        assert files_in(inst.home / left[0]) == files_in_swap
        assert version_in(inst.home / left[0]) == swap_version
        assert any(str(inst.home / left[0]) in s for s in said), said
        ok, detail = doctor_row(inst)
        assert ok and str(inst.home / left[0]) in detail and SAID[row] in detail, detail
    else:
        assert left == []
        ok, detail = doctor_row(inst)
        assert ok, detail
        if route == "a start":  # nothing is left to show: the row says what the start did
            assert SAID[row] in detail, detail
    if route == "a start" and row == "no app":
        assert inst.version_now() == "0.1.15"
    if route == "a start" and row == "no prev":
        assert version_in(inst.home / "app.prev") == "0.1.14"

    # the second time: nothing more is renamed, and nothing more is said
    before, lines_before = folders(inst), len(swap_lines(inst))
    start(inst)
    assert folders(inst) == before and len(swap_lines(inst)) == lines_before


def test_a_swap_folder_held_open_is_left_and_said(
    inst: Install, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A window or a scanner holds `app.swap` (Windows refuses the rename): nothing is renamed and nothing
    in it is deleted; a start says so once, `ns doctor` names it with the way out, a rollback does not start
    over it, and the next start after the window is closed settles it."""
    _all_three(inst)
    swap = inst.home / "app.swap"
    files = files_in(swap)
    settings = read_config(inst.paths)
    real = os.rename

    def held(src: Any, dst: Any, **k: Any) -> None:
        if Path(src).name == "app.swap":
            raise PermissionError(13, "The process cannot access the file", str(src))
        real(src, dst, **k)

    with monkeypatch.context() as m:
        m.setattr(os, "rename", held)
        inst.start()
        first_start.ensure(inst.paths.config)
        first_start.ensure(inst.paths.config)  # the same process again: one line, not two
        lines = swap_lines(inst)
        assert len(lines) == 1 and lines[0]["level"] == "WARN", lines
        assert str(swap) in lines[0]["msg"] and "has it open" in lines[0]["msg"]
        ok, detail = doctor_row(inst)
        assert not ok and str(swap) in detail and "close" in detail.lower(), detail

        with pytest.raises(NsError) as ei:
            update.rollback_app(inst.app, Env().env(), paths=inst.paths)
        assert ei.value.code == "NS-E702" and str(swap) in ei.value.detail
        assert "did not start" in ei.value.detail and "has it open" in ei.value.detail
    assert files_in(swap) == files
    assert not (inst.home / RECORD).exists() and read_config(inst.paths) == settings
    assert inst.version_now() == "0.1.15" and folders(inst) == ["app", "app.prev", "app.swap"]

    start(inst)  # the window is closed: asked again at the next start
    left = [n for n in folders(inst) if LEFT.match(n)]
    assert len(left) == 1 and not swap.exists()
    assert files_in(inst.home / left[0]) == files


def test_an_update_without_a_program_folder_does_not_start_and_deletes_nothing(
    inst: Install, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No `app`, and `app.swap` cannot be put back (held open): an update used to delete `app.prev` to make
    room and then fail, leaving no program at all. It does not start, and says which folder to free."""
    _no_app(inst)
    swap, prev = inst.home / "app.swap", inst.home / "app.prev"
    kept = {str(swap): files_in(swap), str(prev): files_in(prev)}
    copy_before = inst.copy.read_bytes()
    real = os.rename

    def held(src: Any, dst: Any, **k: Any) -> None:
        if Path(src).name == "app.swap":
            raise PermissionError(13, "The process cannot access the file", str(src))
        real(src, dst, **k)

    e = Env()
    with monkeypatch.context() as m:
        m.setattr(os, "rename", held)
        with pytest.raises(NsError) as ei:
            update.update_app(make_zip(tmp_path, "0.1.16"), inst.app, e.env(), paths=inst.paths)
    assert {str(swap): files_in(swap), str(prev): files_in(prev)} == kept
    assert ei.value.code == "NS-E702" and "the update did not start" in ei.value.detail
    assert "Run INSTALL.cmd from the Nightshift zip" in ei.value.detail
    assert "start Nightshift again" not in ei.value.detail  # nothing can start without the folder
    assert str(swap) in ei.value.detail and "has it open" in ei.value.detail
    assert inst.copy.read_bytes() == copy_before and "sync" not in e.calls
    assert not (inst.home / "app.new").exists()

    res = update.update_app(make_zip(tmp_path, "0.1.16"), inst.app, Env().env(), paths=inst.paths)  # freed
    assert inst.version_now() == "0.1.16" and any(SAID["no app"] in n for n in res.notes)


def test_a_folder_in_the_swap_place_that_is_no_finished_program_is_left_alone(inst: Install) -> None:
    """Something named app.swap without a completion marker is not a program Nightshift put there: it is
    neither put in place nor deleted (the rollback used to delete whatever was there), and said."""
    swap = inst.home / "app.swap"
    swap.mkdir()
    (swap / "notes.txt").write_text("mine", encoding="utf-8")
    start(inst)
    lines = swap_lines(inst)
    assert len(lines) == 1 and "left as it is" in lines[0]["msg"] and str(swap) in lines[0]["msg"]
    ok, detail = doctor_row(inst)
    assert not ok and str(swap) in detail and "Move it" in detail
    with pytest.raises(NsError) as ei:
        update.rollback_app(inst.app, Env().env(), paths=inst.paths)
    assert ei.value.code == "NS-E702" and "did not start" in ei.value.detail
    assert (swap / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert inst.version_now() == "0.1.15" and not (inst.home / RECORD).exists()


def test_no_swap_and_no_record_changes_nothing(inst: Install, tmp_path: Path) -> None:
    """Empty: an ordinary install. Nothing is renamed or said, and the doctor row is plain ok."""
    before, settings = folders(inst), read_config(inst.paths)
    start(inst)
    assert folders(inst) == before == ["app", "app.prev"] and read_config(inst.paths) == settings
    assert swap_lines(inst) == []
    assert doctor_row(inst) == (True, "ok")
    res = update.rollback_app(inst.app, Env().env(), paths=inst.paths)
    assert not any("swap" in n for n in res.notes), res.notes
    assert folders(inst) == ["app", "app.prev"] and inst.version_now() == "0.1.14"


# -------------------------------------------------------------------------- 3: a version that cannot be read
def _version_missing(prev: Path) -> None:
    (prev / "VERSION").unlink()


def _version_empty(prev: Path) -> None:
    (prev / "VERSION").write_text("\n", encoding="utf-8")


def _version_garbage(prev: Path) -> None:
    (prev / "VERSION").write_text("dev\n", encoding="utf-8")


UNREADABLE = {"missing": _version_missing, "empty": _version_empty, "garbage": _version_garbage}


@pytest.mark.parametrize("what", sorted(UNREADABLE))
def test_an_unreadable_version_is_treated_alike_whether_missing_empty_or_garbage(
    inst: Install, what: str
) -> None:
    """Box 3: one rule. A version that cannot be read is older than every release, so everything added since
    0.1.14 is taken out; a missing or empty VERSION used to count as the running version (nothing taken out,
    and 0.1.14's strict models then refuse runtime.json)."""
    inst.copy.unlink()  # no copy to put back: the rollback takes out what the older program does not know
    UNREADABLE[what](inst.home / "app.prev")
    cfg = inst.paths.config
    assert json.loads((cfg / "runtime.json").read_text(encoding="utf-8"))["tiny_story"] is True
    assert json.loads((cfg / "answers.json").read_text(encoding="utf-8"))["tiny_story"] is True

    res = inst.rollback()

    for name in ("runtime.json", "answers.json"):  # every NEW_FIELDS field is gone
        assert "tiny_story" not in json.loads((cfg / name).read_text(encoding="utf-8")), name
    older_version_loads_everything(inst.paths)
    assert "the version of the program in app.prev could not be read" in res.settings, res.settings
    assert "runtime.json (tiny_story)" in res.settings and "answers.json (tiny_story)" in res.settings
    assert res.new_version == ("dev" if what == "garbage" else "unknown version")
    assert "0.1.15 does not know" not in res.settings  # never named after the program that ran the rollback


# ------------------------------------------- review of 5511013: no program folder, nothing of ours starts
def _power_loss_after_the_first_rename(inst: Install, monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """`ns update --rollback` from 0.1.15 to 0.1.14 loses power after its first rename: no app, app.swap is
    0.1.15, app.prev is 0.1.14, the record is at `prepared` and the settings are already 0.1.14's. Returns
    the settings as the owner had them before the rollback (they exist only in the record now)."""
    before = read_config(inst.paths)
    with monkeypatch.context() as m:
        _stop_at_rename(2, hard=True)(inst, m)
        with pytest.raises(Stop):
            inst.rollback()
    assert not inst.app.exists() and folders(inst) == ["app.prev", "app.swap"]
    assert json.loads((inst.home / RECORD).read_text(encoding="utf-8"))["stage"] == "prepared"
    assert read_config(inst.paths) != before
    return before


def _held(monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.rename

    def held(src: Any, dst: Any, **k: Any) -> None:
        if Path(src).name == "app.swap":
            raise PermissionError(13, "The process cannot access the file", str(src))
        real(src, dst, **k)

    monkeypatch.setattr(os, "rename", held)


def test_setup_from_a_zip_puts_the_program_back_and_the_owners_settings_survive(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocker 1. With no `app` nothing of Nightshift starts; INSTALL.cmd from a zip is the only code that
    runs. It used to copy a fresh program beside `app.swap`: the first start then set the owner's program
    aside and deleted the record as stale, and the settings from before the rollback were gone."""
    before = _power_loss_after_the_first_rename(inst, monkeypatch)
    swap = inst.home / "app.swap"

    with monkeypatch.context() as m:  # first with the folder held open: nothing is copied, a true way out
        _held(m)
        with pytest.raises(appcopy.AppCopyError) as held:
            appcopy.install_app(inst.source("0.1.16"), inst.home)
    assert held.value.code == "NS-E115" and str(swap) in str(held.value)
    assert "run INSTALL.cmd from the Nightshift zip again" in str(held.value)
    assert "rename app.swap to app" in str(held.value) and "No program was copied" in str(held.value)
    assert "start Nightshift again" not in str(held.value)  # it cannot start
    assert folders(inst) == ["app.prev", "app.swap"] and (inst.home / RECORD).is_file()

    said: list[str] = []
    with pytest.raises(appcopy.AppCopyError) as ei:  # freed: the program goes back, and is not replaced yet
        appcopy.install_app(inst.source("0.1.16"), inst.home, say=said.append)
    assert said[0] == SAID["no app"]  # said first, before anything is copied
    assert ei.value.code == "NS-E115" and SAID["no app"] in str(ei.value)
    assert "Nightshift starts again now" in str(ei.value) and "Start Nightshift once" in str(ei.value)
    assert folders(inst) == ["app", "app.prev"] and inst.version_now() == "0.1.15"
    assert (inst.home / RECORD).is_file()  # never called stale: it holds the owner's settings

    start(inst)  # the shortcut works again: the next start undoes the rollback
    assert read_config(inst.paths) == before and not (inst.home / RECORD).exists()
    appcopy.install_app(inst.source("0.1.16"), inst.home)  # and setup run again now updates
    assert inst.version_now() == "0.1.16" and version_in(inst.home / "app.prev") == "0.1.15"
    snap = config_snapshot.read(inst.copy)
    assert snap.files == {n: b for n, b in before.items() if config_snapshot.valid_name(n)}


def test_setup_started_from_the_swap_folder_itself_says_to_rename_it(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner finds app.swap and runs its INSTALL.cmd: the folder cannot rename itself while it runs."""
    _power_loss_after_the_first_rename(inst, monkeypatch)
    with pytest.raises(appcopy.AppCopyError) as ei:
        appcopy.install_app(inst.home / "app.swap", inst.home)
    assert ei.value.code == "NS-E115" and "rename app.swap to app" in str(ei.value)
    assert "Nothing was changed" in str(ei.value)
    assert folders(inst) == ["app.prev", "app.swap"] and (inst.home / RECORD).is_file()


def test_setup_does_not_copy_beside_a_folder_in_the_swap_place_it_cannot_judge(inst: Install) -> None:
    inst.app.rename(inst.home / "app.swap")
    (inst.home / "app.swap" / appcopy.MARKER).unlink()  # not a program folder Nightshift finished
    with pytest.raises(appcopy.AppCopyError) as ei:
        appcopy.install_app(inst.source("0.1.16"), inst.home)
    assert ei.value.code == "NS-E115" and "left as it is" in str(ei.value)
    assert "rename it to app" in str(ei.value) and "run INSTALL.cmd from the Nightshift zip" in str(ei.value)
    assert folders(inst) == ["app.prev", "app.swap"]


@pytest.mark.parametrize("command", ["ns update", "ns update --rollback"])
def test_a_record_is_never_called_stale_while_there_is_no_program_folder(
    inst: Install, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    """The record is the only place the settings from before the rollback are kept: with no `app` (and
    app.swap held) it stays, and the refusal says the one thing that works: INSTALL.cmd from the zip."""
    _power_loss_after_the_first_rename(inst, monkeypatch)
    record = (inst.home / RECORD).read_bytes()
    with monkeypatch.context() as m:
        _held(m)
        with pytest.raises(NsError) as ei:
            if command == "ns update":
                update.update_app(make_zip(tmp_path, "0.1.16"), inst.app, Env().env(), paths=inst.paths)
            else:
                update.rollback_app(inst.app, Env().env(), paths=inst.paths)
    assert (inst.home / RECORD).read_bytes() == record and folders(inst) == ["app.prev", "app.swap"]
    assert ei.value.code == "NS-E702" and "Its record was kept" in ei.value.detail
    assert "Run INSTALL.cmd from the Nightshift zip" in ei.value.detail
    assert "start Nightshift again" not in ei.value.detail and "Nothing was changed:" not in ei.value.detail


# ------------------------------------------------------------------------ review of 5511013: the notes
def test_ns_doctor_shows_everything_its_start_check_did(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ns doctor` runs the start check: an undone rollback (settings rewritten, record deleted) is in the
    row, not only in the log."""
    before = read_config(inst.paths)
    with monkeypatch.context() as m:
        _stop_at_rename(1)(inst, m)
        with pytest.raises(Stop):
            inst.rollback()
    assert read_config(inst.paths) != before
    inst.start()  # a new process: ns doctor
    ok, detail = doctor_row(inst)
    assert ok and detail.startswith("ok. This check: A rollback to 0.1.14 was interrupted"), detail
    assert "your settings files are back as they were before the rollback" in detail
    assert read_config(inst.paths) == before and not (inst.home / RECORD).exists()
    inst.start()
    ok, detail = doctor_row(inst)  # the next run did nothing: it says what was done last, with its time
    assert ok and detail.startswith("ok. Last: A rollback to 0.1.14 was interrupted") and "UTC)" in detail


def test_ns_doctor_says_it_waits_and_that_it_did_not_look(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another program (a rollback) holds the copy's lock: one line while waiting, and a row that is true
    for doctor (it changes nothing), not the start's sentence about a settings write."""
    monkeypatch.setattr(config_snapshot, "LOCK_WAIT_S", 0.4)
    monkeypatch.setattr(first_start, "WAIT_NOTICE_S", 0.05)
    _all_three(inst)
    waited: list[str] = []
    with config_snapshot.locked(inst.copy) as held:
        assert held
        inst.start()
        ok, detail = doctor_row(inst, lambda: waited.append("waiting"))
    assert waited == ["waiting"]
    assert not ok and "did not look at the program folders" in detail and "Run ns doctor again" in detail
    assert "settings write" not in detail and (inst.home / "app.swap").is_dir()
    inst.start()
    ok, detail = doctor_row(inst, lambda: waited.append("again"))  # the lock is free: no line, and it looks
    assert ok and waited == ["waiting"] and "This check: app.swap was renamed to app.swap-left-" in detail


def test_a_second_rollback_finishes_the_interrupted_one_and_stops(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ns update --rollback` again after one that stopped right after its swap: finishing it and then
    swapping straight back would undo what the owner asked for. It finishes, says so, and stops."""
    now = read_config(inst.paths)
    with monkeypatch.context() as m:
        _stop_at_rename(3, after=True)(inst, m)
        with pytest.raises(Stop):
            inst.rollback()
    e = Env()
    res = update.rollback_app(inst.app, e.env(), paths=inst.paths)
    assert inst.version_now() == "0.1.14" and version_in(inst.home / "app.prev") == "0.1.15"
    assert not (inst.home / RECORD).exists() and "sync" in e.calls
    assert "It was finished now, and nothing else was done: 0.1.14 is the program" in res.settings
    assert "again if you want to go to 0.1.15" in res.settings
    assert res.notes[0] == "finished an interrupted rollback; no program was swapped"
    older_version_loads_everything(inst.paths)
    inst.rollback()  # the owner decides to go forward: 0.1.15 with the settings it had
    assert inst.version_now() == "0.1.15" and read_config(inst.paths) == now


def test_the_state_the_rollback_of_0_1_14_leaves_is_finished_by_the_next_start(inst: Install) -> None:
    """0.1.14 has no record and no recover: its `ns update --rollback` (forward to 0.1.15) that loses power
    after its second rename leaves 0.1.15 in `app`, 0.1.14 in `app.swap` and no `app.prev`. The next start of
    0.1.15 makes app.swap the program backup again and takes the copy of what 0.1.14 left."""
    inst.rollback()  # 0.1.14 is the program
    (inst.paths.config / "dashboard.json").write_text('{"notifications": false}\n', encoding="utf-8")
    on_014 = read_config(inst.paths)
    inst.app.rename(inst.home / "app.swap")  # 0.1.14's own rollback: rename 1,
    (inst.home / "app.prev").rename(inst.app)  # rename 2, and the power goes
    assert inst.version_now() == "0.1.15" and not (inst.home / RECORD).exists()

    start(inst)
    assert folders(inst) == ["app", "app.prev"] and version_in(inst.home / "app.prev") == "0.1.14"
    assert [r["msg"] for r in swap_lines(inst)] == [SAID["no prev"]]
    snap = config_snapshot.read(inst.copy)
    assert (snap.version, snap.taken_by) == ("0.1.14", "first-start")
    assert snap.files == {n: b for n, b in on_014.items() if config_snapshot.valid_name(n)}
    owner_uses_the_new_version(inst.paths)
    inst.rollback()
    assert inst.version_now() == "0.1.14" and read_config(inst.paths) == on_014
    older_version_loads_everything(inst.paths)


def test_a_copy_that_cannot_be_read_does_not_hold_the_record_forever(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale record of a failed restore and a zip that cannot be read: the rollback refuses (the zip may
    be the very copy), and the refusal gives a way out that works: move the zip away."""
    _failed_restore_then_put_in_place_again(inst, monkeypatch)
    aside = inst.home / "moved-away.zip"
    inst.copy.rename(aside)
    inst.copy.mkdir()  # stands for a zip a program holds open: it cannot be read, it may be whole
    with pytest.raises(NsError) as ei:
        update.rollback_app(inst.app, Env().env(), paths=inst.paths)
    assert ei.value.code == "NS-E702" and (inst.home / RECORD).is_file()
    assert f"move {inst.copy.name} out of {inst.home}" in ei.value.detail
    inst.copy.rmdir()  # the owner moved it out
    res = update.rollback_app(inst.app, Env().env(), paths=inst.paths)
    assert not (inst.home / RECORD).exists() and inst.version_now() == "0.1.15"
    assert any("does not belong to the programs installed now" in n for n in res.notes)
