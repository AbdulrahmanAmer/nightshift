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


def doctor_row(inst: Install) -> tuple[bool, str]:
    """The "program folders" row of `ns doctor` (run by the program in <home>/app)."""

    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no engine")

    client = SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(refuse)))
    rows = {name: (ok, detail) for name, ok, detail in doctor.diagnose(inst.paths, client)}
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
    else:
        res = update.rollback_app(inst.app, Env().env(), paths=inst.paths)
        said = res.notes
        assert inst.version_now() == "0.1.14" and version_in(inst.home / "app.prev") == "0.1.15"
        older_version_loads_everything(inst.paths)

    assert not (inst.home / "app.swap").exists()
    assert any(SAID[row] in s for s in said), said
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
