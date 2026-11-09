"""F-10c: after an update made by an OLDER updater, the new version takes the settings copy at its first
start, and `ns update --rollback` always leaves the older version able to start.

0.1.14's `ns update` (Settings > Update, `ns update`, `--github`) takes no settings copy; F-10a's copy is made
only by an updater that has its code. So the update is simulated as 0.1.14 does it: the program folder
becomes `app.prev`, the new one goes in place with its marker, the settings and the copy are not touched.
"First start" is a new process of the new program (`first_start.program_dir` is that program, and
`first_start.reset` makes the next call the first of a process). The rollback is the real one
(`ns_supervisor.update.rollback_app`, the new version's code), and "the older version" is 0.1.14's models,
frozen in tests/frozen_models_0_1_14.py and tests/frozen_config_models_0_1_14.py: every settings file is
loaded with them at the end. Fakes only: a temporary home, fake program folders, no engine, no network.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from installer.stage2 import devmode
from installer.stage2.answers import blank_secrets_in_file, build_answers, load_raw, write_answers
from installer.stage2.state import save_flags
from installer.stage2.steps.s09_shortcuts_scheduler import remember_run_time
from installer.stage2.web import answers_view, appcopy, controller
from ns_common import config_snapshot, first_start
from ns_common.config import (
    load_channel,
    load_install_state,
    load_runtime,
    save_channel,
    save_install_state,
    save_runtime,
)
from ns_common.paths import Paths
from ns_dashboard import formats
from ns_dashboard.state import save_dashboard_settings
from ns_supervisor import backup, schedule_cfg, update
from tests import frozen_config_models_0_1_14 as old_cfg
from tests import frozen_models_0_1_14 as old
from tests.test_installer_app_copy import make_source
from tests.test_ns_update import Env

ROOT = Path(__file__).resolve().parents[1]
FORMAT: dict[str, Any] = {
    "id": "mine",
    "name": "Mine",
    "kind": "short",
    "canvas": {"w": 1080, "h": 1920},
    "duration": {"min_s": 10, "max_s": 60},
    "captions": {},
    "layout": {"regions": [{"name": "main", "w": 1080, "h": 1920}]},
}


@pytest.fixture(autouse=True)
def a_new_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(update, "RENAME_PAUSE_S", 0.01)
    first_start.reset()
    yield
    first_start.reset()


# ------------------------------------------------------------------------------------------ the fake install
def read_config(paths: Paths) -> dict[str, bytes]:
    cfg = paths.config
    return {p.relative_to(cfg).as_posix(): p.read_bytes() for p in sorted(cfg.rglob("*")) if p.is_file()}


def seed(paths: Paths) -> dict[str, bytes]:
    """The settings as 0.1.14 left them (written with 0.1.14's models); returns every file's bytes."""
    cfg = paths.config
    (cfg / "channels").mkdir(parents=True, exist_ok=True)
    (cfg / "formats").mkdir(exist_ok=True)
    state = old.InstallState(
        version="0.1.14",
        app_path=str(paths.home / "app"),
        steps={
            "s01_questions": old.StepState(status="done"),
            "s02_download_bins": old.StepState(status="done"),
        },
        started_at="2026-11-01T10:00:00+00:00",
    )
    answers = old_cfg.InstallerAnswers(
        channel=old_cfg.AnswersChannel(display_name="Demo", niche="true stories")
    )
    files = {
        "runtime.json": json.dumps(old.Runtime().model_dump(mode="json"), indent=2) + "\n",
        "install_state.json": json.dumps(state.model_dump(mode="json"), indent=2) + "\n",
        "install_state.json.bak": json.dumps(state.model_dump(mode="json"), indent=2) + "\n",
        "answers.json": json.dumps(answers.model_dump(mode="json"), indent=2) + "\n",
        "wizard_flags.json": json.dumps({"_install": state.started_at, "llama_pair": "cuda12"}) + "\n",
        "schedule.json": json.dumps(
            {"run_time": "03:00", "task_name": "Nightshift Daily", "mode": "registered"}
        )
        + "\n",
        "dashboard.json": json.dumps({"notifications": True}) + "\n",
        "channels/demo.yaml": yaml.safe_dump(
            old.Channel(slug="demo", display_name="Demo", niche="true stories").model_dump(mode="json"),
            sort_keys=False,
        ),
        "formats/mine.yaml": yaml.safe_dump(FORMAT, sort_keys=False),
        "secrets.dpapi": "blob-of-keys",
    }
    for name, text in files.items():
        (cfg / name).write_text(text, encoding="utf-8", newline="\n")
    return read_config(paths)


def older_version_loads_everything(paths: Paths) -> None:
    """Every settings file loads with 0.1.14's models: the rolled back program can start."""
    cfg = paths.config

    def text(name: str) -> str:
        return (cfg / name).read_text(encoding="utf-8")

    old.Runtime.model_validate_json(text("runtime.json"))
    for name in ("install_state.json", "install_state.json.bak"):
        if (cfg / name).exists():
            old.InstallState.model_validate_json(text(name))
    old_cfg.InstallerAnswers.model_validate_json(text("answers.json"))
    for ch in sorted((cfg / "channels").glob("*.yaml")):
        old.Channel.model_validate(yaml.safe_load(ch.read_text(encoding="utf-8")))
    for f in sorted((cfg / "formats").glob("*.yaml")):
        old_cfg.Format.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")))
    for name in ("wizard_flags.json", "schedule.json", "dashboard.json"):
        if (cfg / name).exists():
            assert isinstance(json.loads(text(name)), dict)


@dataclass
class Install:
    """A home with a program put in place by INSTALL.cmd, then updated the way an older updater does it."""

    tmp: Path
    mp: pytest.MonkeyPatch
    version: str = "0.1.14"
    home: Path = field(init=False)
    paths: Paths = field(init=False)
    app: Path = field(init=False)
    _zips: int = 0

    def __post_init__(self) -> None:
        self.home = self.tmp / "home"
        self.paths = Paths(self.home)
        self.app = self.home / "app"
        appcopy.install_app(self.source(self.version), self.home)

    def source(self, version: str) -> Path:
        self._zips += 1
        return make_source(self.tmp / f"zip-{version}-{self._zips}", version)

    @property
    def copy(self) -> Path:
        return config_snapshot.path_for(self.app)

    def old_updater(self, version: str) -> None:
        """What 0.1.14's `ns update` does to the folders: `app` becomes `app.prev` (an older backup is
        dropped), the new program goes in place with its marker. No settings copy; the settings untouched."""
        new = self.home / "app.new"
        appcopy.copy_tree(self.source(version), new)
        (new / appcopy.MARKER).write_text(appcopy.fingerprint(new), encoding="utf-8")
        prev = self.home / "app.prev"
        if prev.exists():
            shutil.rmtree(prev)
        self.app.rename(prev)
        new.rename(self.app)

    def start(self) -> None:
        """A new process of the program in <home>\\app (the engine, the dashboard, a CLI, setup)."""
        self.mp.setattr(first_start, "program_dir", lambda: self.app)
        first_start.reset()

    def rollback(self) -> update.UpdateResult:
        """`ns update --rollback`, run by the program in <home>\\app."""
        return update.rollback_app(self.app, Env().env(), paths=self.paths)

    def version_now(self) -> str:
        return (self.app / "VERSION").read_text(encoding="utf-8").strip()


@pytest.fixture
def inst(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Install:
    return Install(tmp_path, monkeypatch)


def first_start_writes(paths: Paths) -> None:
    """What the new version does when it starts: it re-saves settings with its own models."""
    save_runtime(paths, load_runtime(paths))
    state = load_install_state(paths)
    state.version = "0.1.15"
    save_install_state(paths, state)
    save_channel(paths, load_channel(paths, "demo"))


def owner_uses_the_new_version(paths: Paths) -> None:
    """The owner changes settings on the new version, including one 0.1.14 does not have."""
    rt = load_runtime(paths)
    rt.tiny_story = True
    save_runtime(paths, rt)
    answers = paths.config / "answers.json"
    write_answers(
        answers, build_answers({**load_raw(answers), "tiny_story": True, "allow_cpu": True}, saved=True)
    )
    save_dashboard_settings(paths, {"notifications": False})


# ------------------------------------------------------------------------------------------ the main promise
def test_the_first_start_after_an_older_updater_takes_the_copy_and_the_rollback_restores_it(
    inst: Install,
) -> None:
    before = seed(inst.paths)
    inst.old_updater("0.1.15")
    assert not inst.copy.exists()  # the older updater kept none: the defect

    inst.start()
    first_start_writes(inst.paths)

    snap = config_snapshot.read(inst.copy)
    assert (snap.taken_by, snap.version) == ("first-start", "0.1.14")
    assert snap.fingerprint == config_snapshot.program_fingerprint(inst.home / "app.prev")
    assert snap.files == {n: b for n, b in before.items() if n != "secrets.dpapi"}  # as 0.1.14 left them
    assert read_config(inst.paths) != before  # the new version did rewrite files

    res = inst.rollback()

    assert inst.version_now() == "0.1.14"
    assert read_config(inst.paths) == before  # byte for byte
    older_version_loads_everything(inst.paths)
    assert "runtime.json" in res.settings and "install_state.json" in res.settings


def test_a_setting_changed_in_between_is_lost_and_the_rollback_names_the_files(inst: Install) -> None:
    before = seed(inst.paths)
    inst.old_updater("0.1.15")
    inst.start()
    first_start_writes(inst.paths)
    owner_uses_the_new_version(inst.paths)
    assert json.loads((inst.paths.config / "runtime.json").read_text(encoding="utf-8"))["tiny_story"] is True

    res = inst.rollback()

    assert read_config(inst.paths) == before
    older_version_loads_everything(inst.paths)
    lost = res.settings.split("anything you changed in them is lost")[-1]
    for name in ("runtime.json", "answers.json", "dashboard.json"):
        assert name in lost, res.settings  # what changed is named, not "some files"


@pytest.mark.parametrize("how", ["deleted", "damaged"])
def test_a_rollback_without_a_usable_copy_removes_what_0_1_14_does_not_know_and_says_so(
    inst: Install, how: str
) -> None:
    seed(inst.paths)
    inst.old_updater("0.1.15")
    inst.start()
    first_start_writes(inst.paths)
    owner_uses_the_new_version(inst.paths)
    if how == "deleted":
        inst.copy.unlink()
    else:
        inst.copy.write_bytes(inst.copy.read_bytes()[:-40])
    with pytest.raises(ValueError):  # the control: as they are, 0.1.14 refuses them
        older_version_loads_everything(inst.paths)

    res = inst.rollback()

    assert inst.version_now() == "0.1.14"
    older_version_loads_everything(inst.paths)
    assert "could not be restored" in res.settings
    assert ("no copy" if how == "deleted" else "damaged") in res.settings
    assert "runtime.json (tiny_story)" in res.settings and "answers.json (tiny_story)" in res.settings
    assert "lost" in res.settings
    # nothing to restore from: every other choice the owner made stays
    assert (
        json.loads((inst.paths.config / "dashboard.json").read_text(encoding="utf-8"))["notifications"]
        is False
    )
    assert json.loads((inst.paths.config / "answers.json").read_text(encoding="utf-8"))["allow_cpu"] is True


def test_the_cli_result_line_says_what_was_removed(inst: Install, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ns update --rollback` (and Settings, which shows the same line) with no copy to restore."""
    from ns_supervisor import cli, control

    seed(inst.paths)
    inst.old_updater("0.1.15")
    inst.start()
    owner_uses_the_new_version(inst.paths)
    inst.copy.unlink()
    monkeypatch.setattr(control, "app_path", lambda p: inst.app)
    monkeypatch.setattr(cli, "_stop_engine", lambda p: None)
    monkeypatch.setattr(cli, "_sync", lambda p, a: None)
    out = cli._apply_update(inst.paths, None, True, False)
    assert out.message.startswith("Now on 0.1.14 (was 0.1.15).")
    assert "runtime.json (tiny_story)" in out.message and "ns restore" in out.message
    older_version_loads_everything(inst.paths)


# ------------------------------------------------------------------------------------------ when it cannot
def log_lines(paths: Paths) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for f in sorted(paths.logs.glob("first_start-*.jsonl")):
        lines += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return lines


def test_a_first_start_that_cannot_save_the_copy_goes_on_and_never_takes_a_late_one(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(inst.paths)
    inst.old_updater("0.1.15")
    real = os.replace

    def disk_full(src: str | Path, dst: str | Path) -> None:
        if str(dst).endswith(".prev-config.zip"):  # the copy is built, but cannot be published
            raise OSError(28, "No space left on device")
        real(src, dst)

    inst.start()
    with monkeypatch.context() as m:
        m.setattr(os, "replace", disk_full)
        owner_uses_the_new_version(inst.paths)  # the write is not stopped

    assert json.loads((inst.paths.config / "runtime.json").read_text(encoding="utf-8"))["tiny_story"] is True
    assert not inst.copy.exists() and not list(inst.home.glob("~ns*.tmp"))
    failed = [r for r in log_lines(inst.paths) if r["event"] == "first_start_copy_failed"]
    assert len(failed) == 1 and failed[0]["level"] == "WARN" and "No space left" in failed[0]["msg"]
    assert "ns update --rollback will have no copy" in failed[0]["msg"].replace("`", "")
    assert first_start.read_state(inst.app)["outcome"] == "no copy"

    inst.start()  # the disk has room again: a later start must not copy what this version already changed
    save_runtime(inst.paths, load_runtime(inst.paths))
    assert not inst.copy.exists()

    res = inst.rollback()
    assert "no copy" in res.settings and "runtime.json (tiny_story)" in res.settings
    older_version_loads_everything(inst.paths)


def _foreign(inst: Install) -> None:
    config_snapshot.save(inst.copy, {}, version="0.1.9", fingerprint="0.1.9|a|b", taken_by="update")


def _own_unrestored(inst: Install) -> None:
    fp = config_snapshot.program_fingerprint(inst.app)
    config_snapshot.save(inst.copy, {}, version="0.1.15", fingerprint=fp, taken_by="rollback")
    config_snapshot.mark_unrestored(inst.copy)


def _own(inst: Install) -> None:
    fp = config_snapshot.program_fingerprint(inst.app)
    config_snapshot.save(inst.copy, {}, version="0.1.15", fingerprint=fp, taken_by="rollback")


def _damaged(inst: Install) -> None:
    _own(inst)
    inst.copy.write_bytes(inst.copy.read_bytes()[:-30])


def _unreadable(inst: Install) -> None:
    inst.copy.mkdir()  # a stand-in for a zip a program holds open: it cannot be read, it may be whole


#: What may be in the copy's place at the first start, and whether the first start may write over it (the
#: rules of an update, `config_snapshot.before_update`).
IN_ITS_PLACE: dict[str, tuple[Callable[[Install], None], bool]] = {
    "a whole copy of another program": (_foreign, False),
    "this program's copy a rollback could not put back": (_own_unrestored, False),
    "a zip that cannot be read": (_unreadable, False),
    "this program's own copy for going forward": (_own, True),
    "a damaged copy": (_damaged, True),
}


@pytest.mark.parametrize("what", sorted(IN_ITS_PLACE))
def test_the_first_start_writes_over_only_what_an_update_may_write_over(inst: Install, what: str) -> None:
    make, replaced = IN_ITS_PLACE[what]
    before = seed(inst.paths)
    inst.old_updater("0.1.15")
    make(inst)
    there = inst.copy.read_bytes() if inst.copy.is_file() else None

    inst.start()
    owner_uses_the_new_version(inst.paths)  # never stopped

    if replaced:
        snap = config_snapshot.read(inst.copy)
        assert snap.taken_by == "first-start" and snap.files == {n: before[n] for n in snap.files}
        assert inst.rollback().settings.count("runtime.json") >= 1
        assert read_config(inst.paths) == before
    else:
        assert (inst.copy.read_bytes() if inst.copy.is_file() else None) == there  # left exactly as it was
        failed = [r for r in log_lines(inst.paths) if r["event"] == "first_start_copy_failed"]
        assert len(failed) == 1 and str(inst.copy) in failed[0]["msg"]
        if inst.copy.is_dir():
            inst.copy.rmdir()
        res = inst.rollback()
        assert "runtime.json (tiny_story)" in res.settings  # no usable copy: what 0.1.14 lacks is taken out
    older_version_loads_everything(inst.paths)


def test_a_settings_file_that_cannot_be_read_at_the_first_start_does_not_stop_the_write(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(inst.paths)
    inst.old_updater("0.1.15")
    real = Path.read_bytes

    def locked(self: Path) -> bytes:
        if self.name == "answers.json":
            raise PermissionError(13, "in use by another program")
        return real(self)

    inst.start()
    with monkeypatch.context() as m:
        m.setattr(Path, "read_bytes", locked)
        save_dashboard_settings(inst.paths, {"notifications": False})
    assert (
        json.loads((inst.paths.config / "dashboard.json").read_text(encoding="utf-8"))["notifications"]
        is False
    )
    assert not inst.copy.exists()
    assert any(
        "answers.json" in r["msg"] for r in log_lines(inst.paths) if r["event"] == "first_start_copy_failed"
    )


def test_a_read_only_settings_file_gets_its_copy_before_the_write_fails_as_it_did(inst: Install) -> None:
    before = seed(inst.paths)
    inst.old_updater("0.1.15")
    runtime = inst.paths.config / "runtime.json"
    os.chmod(runtime, stat.S_IREAD)
    try:
        inst.start()
        with pytest.raises(PermissionError):  # unchanged: a file that cannot be written is reported
            save_runtime(inst.paths, load_runtime(inst.paths))
        assert config_snapshot.read(inst.copy).files["runtime.json"] == before["runtime.json"]
        assert runtime.read_bytes() == before["runtime.json"]
    finally:
        os.chmod(runtime, stat.S_IREAD | stat.S_IWRITE)


# ------------------------------------------------------------------------------------------ once per process
def test_the_guard_decides_once_per_process(inst: Install, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(inst.paths)
    inst.old_updater("0.1.15")
    calls = {"read": 0, "capture": 0, "program": 0}
    real_read, real_capture = config_snapshot.read, config_snapshot.capture

    def counting_read(path: Path) -> config_snapshot.Snapshot:
        calls["read"] += 1
        return real_read(path)

    def counting_capture(config: Path, only: object = None) -> dict[str, bytes]:
        calls["capture"] += 1
        return real_capture(config)

    def program() -> Path:
        calls["program"] += 1
        return inst.app

    monkeypatch.setattr(config_snapshot, "read", counting_read)
    monkeypatch.setattr(config_snapshot, "capture", counting_capture)
    inst.start()
    monkeypatch.setattr(first_start, "program_dir", program)

    for _ in range(3):
        first_start_writes(inst.paths)
        save_flags(inst.paths, {"x": 1})
        schedule_cfg.write_run_time(inst.paths, "04:00")
    assert calls == {"read": 1, "capture": 1, "program": 1}  # after the first call: no file access at all

    first_start.reset()  # the next process finds the copy and leaves it
    kept = inst.copy.read_bytes()
    save_runtime(inst.paths, load_runtime(inst.paths))
    assert calls == {"read": 2, "capture": 1, "program": 2}
    assert inst.copy.read_bytes() == kept


# ------------------------------------------------------------------------------------------ every writer
def _blank_secrets_prepare(paths: Paths) -> None:
    f = paths.config / "answers.json"
    f.write_text(
        json.dumps({**json.loads(f.read_text(encoding="utf-8")), "pexels_api_key": "k"}), encoding="utf-8"
    )


def _backup_prepare(paths: Paths) -> None:
    backup.make_backup(paths, paths.home.parent / "backup.zip")


WRITERS: dict[str, tuple[Callable[[Paths], object], Callable[[Paths], object] | None]] = {
    "save_runtime (engine, Settings, ns tune, ns models get, setup)": (
        lambda p: save_runtime(p, load_runtime(p)),
        None,
    ),
    "save_install_state (setup steps)": (lambda p: save_install_state(p, load_install_state(p)), None),
    "save_channel (channel editor, studio channels, setup)": (
        lambda p: save_channel(p, load_channel(p, "demo")),
        None,
    ),
    "setup: write_answers": (
        lambda p: write_answers(
            p.config / "answers.json", build_answers(load_raw(p.config / "answers.json"), saved=True)
        ),
        None,
    ),
    "setup: blank_secrets_in_file": (
        lambda p: blank_secrets_in_file(p.config / "answers.json"),
        _blank_secrets_prepare,
    ),
    "setup: save_flags": (lambda p: save_flags(p, {"x": 1}), None),
    "setup: remember_run_time": (lambda p: remember_run_time(p, "04:00"), None),
    "setup window: answers_view.write_answers": (
        lambda p: answers_view.write_answers(
            p.config / "answers.json", json.loads((p.config / "answers.json").read_text(encoding="utf-8"))
        ),
        None,
    ),
    "setup window: write_step": (
        lambda p: controller.write_step(p.home, "0.1.15", p.home / "app", "s03_x", "done", None),
        None,
    ),
    "setup window: reset_install_times": (lambda p: controller.reset_install_times(p.home), None),
    "setup window: dev mode switch": (lambda p: devmode.reset_for_mode_switch(p.home, True), None),
    "dashboard: Settings": (lambda p: save_dashboard_settings(p, {"notifications": False}), None),
    "dashboard: Library > Formats": (lambda p: formats._write(p, "mine", {**FORMAT, "name": "Mine 2"}), None),
    "ns schedule install": (lambda p: schedule_cfg.write_run_time(p, "04:00"), None),
    "ns schedule uninstall": (lambda p: schedule_cfg.clear_run_time(p), None),
    "ns restore": (lambda p: backup.restore_backup(p, p.home.parent / "backup.zip"), _backup_prepare),
}


@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_every_writer_takes_the_copy_before_it_changes_anything(inst: Install, writer: str) -> None:
    write, prepare = WRITERS[writer]
    seed(inst.paths)
    if prepare is not None:
        prepare(inst.paths)
    before = read_config(inst.paths)
    inst.old_updater("0.1.15")
    inst.start()

    write(inst.paths)

    snap = config_snapshot.read(inst.copy)
    assert snap.taken_by == "first-start"
    assert snap.files == {n: b for n, b in before.items() if config_snapshot.valid_name(n)}


# ------------------------------------------------------------------------------------------ other routes
def test_a_copy_made_by_install_cmd_or_the_new_updater_is_never_replaced(inst: Install) -> None:
    seed(inst.paths)
    appcopy.install_app(inst.source("0.1.15"), inst.home)  # INSTALL.cmd of the new zip takes its own copy
    made = inst.copy.read_bytes()
    assert config_snapshot.read(inst.copy).taken_by == "installer"
    inst.start()
    first_start_writes(inst.paths)
    owner_uses_the_new_version(inst.paths)
    assert inst.copy.read_bytes() == made


def test_two_updates_by_the_older_updater_before_any_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst = Install(tmp_path, monkeypatch, version="0.1.13")
    seed(inst.paths)
    inst.old_updater("0.1.14")  # 0.1.14 has no first-start code: it takes nothing
    (inst.paths.config / "dashboard.json").write_text(
        '{"notifications": false}\n', encoding="utf-8"
    )  # set on 0.1.14
    at_014 = read_config(inst.paths)
    inst.old_updater("0.1.15")
    inst.start()
    first_start_writes(inst.paths)
    owner_uses_the_new_version(inst.paths)

    res = inst.rollback()

    assert inst.version_now() == "0.1.14"  # only the latest pair is kept
    assert read_config(inst.paths) == at_014  # the settings 0.1.14 had, with what was set on it
    older_version_loads_everything(inst.paths)
    assert "runtime.json" in res.settings


def test_a_failed_update_by_the_older_updater_leaves_a_copy_the_next_try_does_not_reuse(
    inst: Install,
) -> None:
    """0.1.14's update runs `ns verify --quick` with the new code (a first start) and, when that fails, puts
    0.1.14 back without touching the copy. The copy of that try is stale by the next try."""
    seed(inst.paths)
    inst.old_updater("0.1.15")
    inst.start()
    first_start_writes(inst.paths)  # the verify of the failed try
    assert inst.copy.exists()
    # 0.1.14's _restore: the new tree goes, the old one comes back; the copy stays behind
    shutil.rmtree(inst.app)
    (inst.home / "app.prev").rename(inst.app)
    (inst.paths.config / "dashboard.json").write_text(
        '{"notifications": false}\n', encoding="utf-8"
    )  # on 0.1.14
    on_014 = read_config(inst.paths)

    inst.old_updater("0.1.15")  # the next try
    inst.start()
    first_start_writes(inst.paths)
    res = inst.rollback()

    assert read_config(inst.paths) == on_014  # not the settings of the first try
    older_version_loads_everything(inst.paths)
    assert "runtime.json" in res.settings


def test_going_forward_with_the_older_rollback_and_back_again(inst: Install) -> None:
    """0.1.15's rollback leaves 0.1.14 running; 0.1.14's own `ns update --rollback` goes forward again without
    knowing about copies. The next first start of 0.1.15 takes a new one."""
    seed(inst.paths)
    inst.old_updater("0.1.15")
    inst.start()
    first_start_writes(inst.paths)
    inst.rollback()
    (inst.paths.config / "dashboard.json").write_text(
        '{"notifications": false}\n', encoding="utf-8"
    )  # on 0.1.14
    on_014 = read_config(inst.paths)
    # 0.1.14's rollback: swap the folders, write the marker, nothing else
    swap = inst.home / "app.swap"
    inst.app.rename(swap)
    (inst.home / "app.prev").rename(inst.app)
    swap.rename(inst.home / "app.prev")
    (inst.app / appcopy.MARKER).write_text(appcopy.fingerprint(inst.app), encoding="utf-8")
    assert inst.version_now() == "0.1.15"

    inst.start()
    owner_uses_the_new_version(inst.paths)
    res = inst.rollback()

    assert inst.version_now() == "0.1.14"
    assert read_config(inst.paths) == on_014
    older_version_loads_everything(inst.paths)
    assert "runtime.json" in res.settings


def test_nothing_is_taken_without_a_program_backup_or_for_the_settings_of_another_home(
    inst: Install, tmp_path: Path
) -> None:
    seed(inst.paths)
    inst.start()  # a fresh install: no app.prev
    first_start_writes(inst.paths)
    assert not inst.copy.exists() and not (inst.app / first_start.STATE_NAME).exists()

    inst.old_updater("0.1.15")
    inst.start()
    other = Paths(tmp_path / "elsewhere")  # a home whose program is not the one running
    seed(other)
    save_runtime(other, load_runtime(other))
    assert not inst.copy.exists()
    assert not list((tmp_path / "elsewhere").glob("*.zip"))


def test_the_three_fingerprints_agree(tmp_path: Path) -> None:
    folder = make_source(tmp_path / "p", "0.1.15")
    assert (
        config_snapshot.program_fingerprint(folder)
        == update.app_fingerprint(folder)
        == appcopy.fingerprint(folder)
    )
    assert config_snapshot.program_fingerprint(tmp_path / "missing") == "-|-|-"


# ------------------------------------------------------------------------------- an interrupted rollback
class Stop(BaseException):
    """The process ends here (Ctrl+C, the window closed): not an Exception, so nothing in the product
    catches it as an error."""


def the_new_version_loads_everything(paths: Paths) -> None:
    """The newer program (this code) loads every settings file: it reads both shapes."""
    load_runtime(paths)
    load_install_state(paths)
    load_channel(paths, "demo")
    build_answers(load_raw(paths.config / "answers.json"), saved=True)


@dataclass
class Point:
    """Where a rollback is interrupted, and what the folders look like then."""

    arm: Callable[[Install, pytest.MonkeyPatch], None]
    settings_ready: bool  # the settings were already made readable by 0.1.14 at this point
    older_in_place: bool  # 0.1.14 is in <home>\app afterwards
    no_program: bool = False  # a hard stop with no program in <home>\app (the record's help says what to do)


def _stop_at_the_start_of_the_settings(inst: Install, mp: pytest.MonkeyPatch) -> None:
    def stop(*a: object, **k: object) -> object:
        assert (inst.home / "app.rollback-pending.json").is_file()  # the record is already written
        raise Stop

    mp.setattr(update, "_prepare_settings", stop)


def _stop_at_settings_write(n: int) -> Callable[[Install, pytest.MonkeyPatch], None]:
    """Stop before the n-th write of a settings file (a restore, or taking out what 0.1.14 lacks)."""

    def arm(inst: Install, mp: pytest.MonkeyPatch) -> None:
        count = {"n": 0}
        real_bytes, real_text = config_snapshot.write_bytes_atomic, update.write_text_atomic

        def check(path: Path) -> None:
            if inst.paths.config in Path(path).parents:
                count["n"] += 1
                if count["n"] == n:
                    raise Stop

        def wb(path: Path, data: bytes) -> Path:
            check(path)
            return real_bytes(path, data)

        def wt(path: Path, text: str, *a: Any, **k: Any) -> Path:
            check(path)
            return real_text(path, text, *a, **k)

        mp.setattr(config_snapshot, "write_bytes_atomic", wb)
        mp.setattr(update, "write_text_atomic", wt)

    return arm


Arm = Callable[[Install, pytest.MonkeyPatch], None]


def _stop_at_rename(n: int, *, after: bool = False, hard: bool = False) -> Arm:
    """Stop at the n-th folder rename of the swap (before it, or right after it). `hard`: a power loss, so
    the rollback's own clean-up of the folders does not run either."""

    def arm(inst: Install, mp: pytest.MonkeyPatch) -> None:
        count = {"n": 0}
        real = update.rename_with_retry

        def rename(src: Path, dst: Path, **k: Any) -> None:
            count["n"] += 1
            if count["n"] == n and not after:
                raise Stop
            real(src, dst, **k)
            if count["n"] == n and after:
                raise Stop

        mp.setattr(update, "rename_with_retry", rename)
        if hard:
            mp.setattr(update, "_settle_folders", lambda *a: None)

    return arm


POINTS: dict[str, Point] = {
    "1 after the record, before the settings": Point(_stop_at_the_start_of_the_settings, False, False),
    "2 in the middle of the settings": Point(_stop_at_settings_write(2), False, False),
    "3 after the settings, before the swap": Point(_stop_at_rename(1), True, False),
    "4 after the first rename": Point(_stop_at_rename(2), True, False),
    "4h power loss after the first rename": Point(
        _stop_at_rename(2, hard=True), True, False, no_program=True
    ),
    "5 after the second rename": Point(_stop_at_rename(3), True, True),
    "5h power loss after the second rename": Point(_stop_at_rename(3, hard=True), True, True),
    "6 after the swap, before the copy for going forward": Point(_stop_at_rename(3, after=True), True, True),
}


def _installed(inst: Install, with_copy: bool) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """0.1.14 updated to 0.1.15 by the older updater; 0.1.15 started and the owner used a new setting.
    Returns (the settings as 0.1.14 left them, the settings now)."""
    before = seed(inst.paths)
    inst.old_updater("0.1.15")
    inst.start()
    first_start_writes(inst.paths)
    owner_uses_the_new_version(inst.paths)
    if not with_copy:
        inst.copy.unlink()
    with pytest.raises(ValueError):  # the control: as they are now, 0.1.14 refuses them
        older_version_loads_everything(inst.paths)
    return before, read_config(inst.paths)


@pytest.mark.parametrize("with_copy", [True, False], ids=["copy", "no-copy"])
@pytest.mark.parametrize("where", sorted(POINTS))
def test_a_rollback_interrupted_anywhere_leaves_settings_the_program_in_place_loads(
    inst: Install, monkeypatch: pytest.MonkeyPatch, where: str, with_copy: bool
) -> None:
    """Head order F-10 / break pass finding 1: the rollback runs the newer code, but the next start is the
    OLDER program's, which has no rollback code. At every point an interruption leaves either 0.1.14 in place
    with settings 0.1.14 loads (checked with its frozen models), or 0.1.15 in place, whose next start puts
    the settings back as they were; and either way `ns update --rollback` then completes."""
    point = POINTS[where]
    before, now = _installed(inst, with_copy)
    record = inst.home / "app.rollback-pending.json"

    with monkeypatch.context() as m:
        point.arm(inst, m)
        with pytest.raises(Stop):
            inst.rollback()

    # first what the next start meets: the program in <home>pp and settings it loads
    if point.settings_ready:  # the state 0.1.14 finds, whichever program is put in place by hand
        older_version_loads_everything(inst.paths)
    if point.no_program:  # the instant between two renames (a power loss): no program in <home>pp
        assert not inst.app.exists() and (inst.home / "app.swap").is_dir()
    elif point.older_in_place:
        assert inst.version_now() == "0.1.14"
        older_version_loads_everything(inst.paths)  # 0.1.14 starts: it has no code to finish anything
    else:
        assert inst.version_now() == "0.1.15"
        the_new_version_loads_everything(inst.paths)  # 0.1.15 is in place and reads what it finds
    assert record.is_file()  # written first, still there: the next start knows
    if point.no_program:  # the record says what to rename by hand
        assert "rename app.swap back to app" in record.read_text(encoding="utf-8")
        (inst.home / "app.swap").rename(inst.app)

    if point.older_in_place:
        # a later older version that has this code finishes the rollback at its first start
        inst.start()
        first_start.ensure(inst.paths.config)
        assert not record.exists()
        assert (inst.home / "app.prev" / "VERSION").read_text(encoding="utf-8").strip() == "0.1.15"
        forward = config_snapshot.read(inst.copy)
        assert (forward.version, forward.taken_by) == ("0.1.15", "rollback")
        assert forward.files == {n: b for n, b in now.items() if config_snapshot.valid_name(n)}
        older_version_loads_everything(inst.paths)
        inst.rollback()  # and going forward again gives 0.1.15 the settings it had
        assert inst.version_now() == "0.1.15" and read_config(inst.paths) == now
        return

    assert inst.version_now() == "0.1.15"
    inst.start()  # its next start (the engine, the dashboard, a CLI) undoes the rollback
    first_start.ensure(inst.paths.config)
    assert not record.exists()
    assert read_config(inst.paths) == now  # back exactly as before the rollback
    said = [r for r in log_lines(inst.paths) if r["event"] == "rollback_recovered"]
    assert len(said) == 1 and "ns update --rollback again" in said[0]["msg"]

    res = inst.rollback()  # and the rollback run again completes
    assert inst.version_now() == "0.1.14" and not record.exists()
    older_version_loads_everything(inst.paths)
    if with_copy:
        assert read_config(inst.paths) == before
    else:
        assert "runtime.json (tiny_story)" in res.settings


def test_the_rollback_finishes_an_interrupted_one_before_it_starts(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ns update --rollback` run again right after an interruption (no other start in between)."""
    before, now = _installed(inst, True)
    with monkeypatch.context() as m:
        _stop_at_settings_write(3)(inst, m)
        with pytest.raises(Stop):
            inst.rollback()
    res = inst.rollback()
    assert inst.version_now() == "0.1.14" and read_config(inst.paths) == before
    assert any("interrupted" in n for n in res.notes)
    assert not (inst.home / "app.rollback-pending.json").exists()
    inst.rollback()  # forward again: what 0.1.15 had before the first try
    assert read_config(inst.paths) == now


def test_a_rollback_whose_record_cannot_be_written_changes_nothing(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, now = _installed(inst, True)

    def full(app: Path, intent: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(update.rollback_intent, "write", full)
    with pytest.raises(update.NsError) as ei:
        inst.rollback()
    assert ei.value.code == "NS-E702" and "Nothing was changed" in ei.value.detail
    assert inst.version_now() == "0.1.15" and read_config(inst.paths) == now


def test_the_engine_start_finishes_an_interrupted_rollback_before_it_reads_a_setting(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    import sys
    import types

    from ns_supervisor import app as engine

    _, now = _installed(inst, True)
    with monkeypatch.context() as m:
        _stop_at_rename(1)(inst, m)
        with pytest.raises(Stop):
            inst.rollback()
    assert read_config(inst.paths) != now  # 0.1.14's settings, 0.1.15 in place

    class Seen(Exception):
        pass

    seen: dict[str, object] = {}

    def load(paths: Paths) -> object:
        seen["record"] = (inst.home / "app.rollback-pending.json").exists()
        seen["settings"] = read_config(paths)
        raise Seen

    inst.start()
    monkeypatch.setitem(sys.modules, "uvicorn", types.ModuleType("uvicorn"))
    monkeypatch.setattr(engine, "load_runtime", load)
    with pytest.raises(Seen):
        engine._serve(argparse.Namespace(port=None, headless=True), inst.paths)
    assert seen == {"record": False, "settings": now}


# --------------------------------------------------------------------------------- two processes at once
def test_two_processes_starting_at_once_never_save_the_newer_settings_as_the_older_copy(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break pass finding 4: process B looks at the copy's place before A has published, A publishes and
    changes a setting, and B then captures the changed settings and publishes them as 0.1.14's copy. With
    the cross-process lock B waits, looks again, and leaves A's copy alone."""
    before = seed(inst.paths)
    inst.old_updater("0.1.15")
    inst.start()
    config = inst.paths.config
    b_looked, a_changed = threading.Event(), threading.Event()
    threads: list[threading.Thread] = []
    errors: list[BaseException] = []
    real_look, real_capture = config_snapshot.look, config_snapshot.capture
    real_publish = config_snapshot.publish

    def process_b() -> None:  # a second process: its own first-start check, nothing shared with A
        try:
            first_start._first_start(config)
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    def look(path: Path) -> config_snapshot.Slot:
        slot = real_look(path)
        if threading.current_thread().name == "B":
            b_looked.set()
        return slot

    def capture(cfg: Path, only: object = None) -> dict[str, bytes]:
        if threading.current_thread().name == "B":
            a_changed.wait(5)  # B reads the settings after A changed them
        return real_capture(cfg)

    def publish(path: Path, data: bytes) -> config_snapshot.Snapshot:
        if threading.current_thread().name == "A":
            b = threading.Thread(target=process_b, name="B")
            threads.append(b)
            b.start()
            b_looked.wait(1)  # without the lock, B looks at the empty place now
        return real_publish(path, data)

    def process_a() -> None:
        try:
            first_start._first_start(config)  # takes the copy
            rt = json.loads((config / "runtime.json").read_text(encoding="utf-8"))
            (config / "runtime.json").write_text(json.dumps({**rt, "tiny_story": True}), encoding="utf-8")
            a_changed.set()
        except BaseException as e:  # pragma: no cover - reported below
            errors.append(e)

    monkeypatch.setattr(config_snapshot, "look", look)
    monkeypatch.setattr(config_snapshot, "capture", capture)
    monkeypatch.setattr(config_snapshot, "publish", publish)
    a = threading.Thread(target=process_a, name="A")
    a.start()
    a.join(30)
    for t in threads:
        t.join(30)
    assert not errors

    snap = config_snapshot.read(inst.copy)
    assert snap.files == {n: b for n, b in before.items() if config_snapshot.valid_name(n)}  # as 0.1.14 left
    assert b"tiny_story" not in snap.files["runtime.json"]


# ------------------------------------------------------------------------------------- nowhere to record
def test_when_neither_the_copy_nor_the_program_folder_can_be_written_no_later_start_takes_a_late_copy(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break pass finding 5: the first start could write neither the zip nor `<app>/.ns-first-start.json`.
    The record goes to the settings folder instead, so the next start (disk fine again) takes no copy of
    settings 0.1.15 already changed, and the rollback says it has none."""
    seed(inst.paths)
    inst.old_updater("0.1.15")
    real = os.replace
    in_program = inst.app / first_start.STATE_NAME

    def refused(src: str | Path, dst: str | Path) -> None:
        if str(dst).endswith(".prev-config.zip") or Path(dst) == in_program:
            raise OSError(28, "No space left on device")
        real(src, dst)

    inst.start()
    with monkeypatch.context() as m:
        m.setattr(os, "replace", refused)
        owner_uses_the_new_version(inst.paths)
    assert not inst.copy.exists() and not in_program.exists()

    inst.start()
    save_runtime(inst.paths, load_runtime(inst.paths))
    assert not inst.copy.exists()  # no late copy
    res = inst.rollback()
    assert "no copy" in res.settings and "runtime.json (tiny_story)" in res.settings
    older_version_loads_everything(inst.paths)
    failed = [r for r in log_lines(inst.paths) if r["event"] == "first_start_copy_failed"]
    assert len(failed) == 1 and str(inst.paths.config / first_start.STATE_NAME) in failed[0]["msg"]


def test_when_no_record_can_be_written_anywhere_the_log_line_still_stops_a_late_copy(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(inst.paths)
    inst.old_updater("0.1.15")
    real = os.replace

    def refused(src: str | Path, dst: str | Path) -> None:
        if str(dst).endswith((".prev-config.zip", first_start.STATE_NAME)):
            raise OSError(28, "No space left on device")
        real(src, dst)

    inst.start()
    with monkeypatch.context() as m:
        m.setattr(os, "replace", refused)
        owner_uses_the_new_version(inst.paths)

    inst.start()
    save_runtime(inst.paths, load_runtime(inst.paths))
    assert not inst.copy.exists()  # the log line alone stops the late copy
    failed = [r for r in log_lines(inst.paths) if r["event"] == "first_start_copy_failed"]
    assert len(failed) == 1 and "could not be recorded either" in failed[0]["msg"]
    assert "a later start may save" in failed[0]["msg"]  # says plainly what happens without that line


# --------------------------------------------------------------------------------------- honest messages
def test_a_rollback_with_no_copy_and_nothing_to_take_out_does_not_say_something_failed(inst: Install) -> None:
    """Break pass finding 6: rolled back before 0.1.15 saved anything: nothing needed restoring."""
    before = seed(inst.paths)
    inst.old_updater("0.1.15")
    res = inst.rollback()
    assert read_config(inst.paths) == before
    assert "could not be restored" not in res.settings and "ns restore" not in res.settings
    assert "There was no copy of your settings files to put back" in res.settings
    assert "none of them holds a setting 0.1.14 does not know" in res.settings


def test_a_file_the_rollback_cannot_change_is_named_and_never_said_to_be_clean(inst: Install) -> None:
    seed(inst.paths)
    inst.old_updater("0.1.15")
    inst.start()
    rt = load_runtime(inst.paths)
    rt.tiny_story = True
    save_runtime(inst.paths, rt)  # (the first start takes the copy here)
    inst.copy.unlink()
    runtime = inst.paths.config / "runtime.json"
    os.chmod(runtime, stat.S_IREAD)
    try:
        res = inst.rollback()
    finally:
        os.chmod(runtime, stat.S_IREAD | stat.S_IWRITE)
    assert "none of them holds a setting" not in res.settings
    assert "0.1.14 may refuse them: runtime.json" in res.settings and "ns restore" in res.settings


# ------------------------------------------------------------------------------- the record after a rollback
RECORD = "app.rollback-pending.json"


def _interrupted(inst: Install, monkeypatch: pytest.MonkeyPatch, arm: Arm) -> tuple[dict[str, bytes], ...]:
    """0.1.14 -> 0.1.15 by the older updater, settings used, then a rollback stopped by `arm`."""
    before, now = _installed(inst, True)
    with monkeypatch.context() as m:
        arm(inst, m)
        with pytest.raises(Stop):
            inst.rollback()
    assert (inst.home / RECORD).is_file()
    return before, now


def test_a_record_left_by_an_earlier_install_of_the_same_build_is_stale_not_undone(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass 2, item 1: the rollback stopped after the swap; 0.1.14 ran; the owner then updated to the SAME
    0.1.15 build with 0.1.14's updater. The fingerprint matches the record's "from", but it is another
    install: undoing would put back 0.1.15's old settings and the first start would then save them as
    0.1.14's copy. The record is stale; the copy is of what 0.1.14 left."""
    _interrupted(inst, monkeypatch, _stop_at_rename(3, after=True))
    assert inst.version_now() == "0.1.14"
    on_014 = read_config(inst.paths)  # 0.1.14 runs and changes nothing
    left_by_014 = {n: b for n, b in on_014.items() if config_snapshot.valid_name(n)}
    older_version_loads_everything(inst.paths)
    built = config_snapshot.program_fingerprint(inst.home / "app.prev")

    inst.old_updater("0.1.15")  # the same build again: same fingerprint, a new install
    assert config_snapshot.program_fingerprint(inst.app) == built
    inst.start()
    first_start_writes(inst.paths)

    assert not (inst.home / RECORD).exists()
    snap = config_snapshot.read(inst.copy)
    assert (snap.version, snap.taken_by) == ("0.1.14", "first-start")
    assert snap.files == left_by_014  # what 0.1.14 left, not 0.1.15's settings from before the rollback
    stale = [r for r in log_lines(inst.paths) if r["event"] == "rollback_recovered"]
    assert len(stale) == 1 and "does not belong to the programs installed now" in stale[0]["msg"]
    assert "is still the program" not in stale[0]["msg"]

    res = inst.rollback()
    assert inst.version_now() == "0.1.14" and read_config(inst.paths) == on_014
    assert "back as 0.1.14 left them" in res.settings
    older_version_loads_everything(inst.paths)


def test_an_undo_that_is_itself_interrupted_is_resumed_by_the_next_start(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass 2, item 2: the next start's undo stops after one file; the start after that must finish it, not
    take the half-undone settings for a program having run on them."""
    _, now = _interrupted(inst, monkeypatch, _stop_at_rename(1))
    inst.start()
    with monkeypatch.context() as m:
        _stop_at_settings_write(2)(inst, m)
        with pytest.raises(Stop):
            first_start.ensure(inst.paths.config)
    assert read_config(inst.paths) != now  # half undone

    inst.start()
    first_start.ensure(inst.paths.config)
    assert not (inst.home / RECORD).exists()
    assert read_config(inst.paths) == now


def test_install_cmd_never_saves_a_copy_of_half_prepared_settings(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass 2, item 3: the rollback stopped before the swap (0.1.15 in place, settings half way to 0.1.14's),
    then INSTALL.cmd of 0.1.16. It refuses (nothing replaced, the record kept) until 0.1.15 has put the
    settings back; then the copy it saves is 0.1.15's real settings."""
    _, now = _interrupted(inst, monkeypatch, _stop_at_rename(1))
    copy_before = inst.copy.read_bytes()
    with pytest.raises(appcopy.AppCopyError) as ei:
        appcopy.install_app(inst.source("0.1.16"), inst.home)
    assert ei.value.code == "NS-E115" and "not replaced" in str(ei.value)
    assert inst.version_now() == "0.1.15" and (inst.home / RECORD).is_file()
    assert inst.copy.read_bytes() == copy_before and not (inst.home / "app.new").exists()

    inst.start()
    first_start.ensure(inst.paths.config)  # 0.1.15 starts once: settings back as they were
    appcopy.install_app(inst.source("0.1.16"), inst.home)
    assert inst.version_now() == "0.1.16"
    snap = config_snapshot.read(inst.copy)
    assert snap.version == "0.1.15"
    assert snap.files == {n: b for n, b in now.items() if config_snapshot.valid_name(n)}


def test_install_cmd_finishes_a_rollback_whose_older_program_is_in_place(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    _interrupted(inst, monkeypatch, _stop_at_rename(3, after=True))
    on_014 = read_config(inst.paths)
    said: list[str] = []
    appcopy.install_app(inst.source("0.1.16"), inst.home, say=said.append)
    assert not (inst.home / RECORD).exists() and any("finished" in s for s in said)
    snap = config_snapshot.read(inst.copy)
    assert snap.version == "0.1.14"
    assert snap.files == {n: b for n, b in on_014.items() if config_snapshot.valid_name(n)}


@pytest.mark.parametrize("command", ["ns update", "ns update --rollback"])
def test_an_update_or_rollback_refuses_when_an_interrupted_rollback_cannot_be_undone(
    inst: Install, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    """Pass 2, item 4: the settings cannot be put back (a locked file): going on would save or overwrite
    half-prepared settings, and the next version would drop the record. NS-E702, nothing changed."""
    from tests.test_ns_update import make_zip

    _interrupted(inst, monkeypatch, _stop_at_rename(1))
    half = read_config(inst.paths)

    def locked(*a: object, **k: object) -> object:
        raise config_snapshot.SnapshotError("could not restore runtime.json: in use by another program")

    e = Env()
    with monkeypatch.context() as m:
        m.setattr(config_snapshot, "restore", locked)
        with pytest.raises(update.NsError) as ei:
            if command == "ns update":
                update.update_app(make_zip(tmp_path, "0.1.16"), inst.app, e.env(), paths=inst.paths)
            else:
                update.rollback_app(inst.app, e.env(), paths=inst.paths)
    assert ei.value.code == "NS-E702" and "Nothing was changed" in ei.value.detail
    assert "in use by another program" in ei.value.detail
    assert inst.version_now() == "0.1.15" and read_config(inst.paths) == half
    assert (inst.home / RECORD).is_file()  # kept for the next try
    assert "sync" not in e.calls

    inst.start()
    first_start.ensure(inst.paths.config)  # the file is free again
    assert not (inst.home / RECORD).exists()


def test_the_undo_says_why_the_settings_were_not_saved(
    inst: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass 2, item 5: the settings were read but their copy could not be made; the undo must not say they
    could not be read."""
    _installed(inst, True)
    real = config_snapshot.build

    def no_copy(files: dict[str, bytes], **k: Any) -> bytes:
        if k.get("taken_by") == "rollback":
            raise config_snapshot.SnapshotError("the copy did not read back as it was written")
        return real(files, **k)

    with monkeypatch.context() as m:
        m.setattr(config_snapshot, "build", no_copy)
        _stop_at_rename(1)(inst, m)
        with pytest.raises(Stop):
            inst.rollback()
    inst.start()
    first_start.ensure(inst.paths.config)
    said = [r["msg"] for r in log_lines(inst.paths) if r["event"] == "rollback_recovered"]
    assert len(said) == 1 and "a copy of them could not be made" in said[0]
    assert "could not be read" not in said[0]


# ------------------------------------------------------------------------------------------ the writer scan
PACKAGES = ("ns_common", "ns_supervisor", "ns_dashboard", "ns_studio", "ns_tts", "installer")
SETTINGS_ATTRS = {"runtime_file", "install_state_file", "channel_file", "channels_dir", "formats_dir"}
#: a function that changes files -> the positions of the arguments naming a file it writes or removes
FILE_WRITERS: dict[str, tuple[int, ...]] = {
    "write_text_atomic": (0,),
    "write_bytes_atomic": (0,),
    "write_stream_atomic": (0,),
    "write_chunks_atomic": (0,),
    "write_zip_atomic": (0,),
    "append_to_zip_atomic": (0,),
    "copy_atomic": (1,),
    "quarantine": (0,),
    "replace_atomic": (0, 1),
}
QUALIFIED: dict[tuple[str, str], tuple[int, ...]] = {
    ("os", "replace"): (0, 1),
    ("os", "rename"): (0, 1),
    ("os", "remove"): (0,),
    ("os", "unlink"): (0,),
    ("shutil", "move"): (0, 1),
    ("shutil", "copy"): (1,),
    ("shutil", "copy2"): (1,),
    ("shutil", "copyfile"): (1,),
    ("shutil", "copytree"): (1,),
    ("shutil", "rmtree"): (0,),
}
FILE_METHODS = {"write_text", "write_bytes", "unlink", "rename", "touch"}  # the object is the file
GUARD_CALLS = {"guard", "ensure", "settled"}
PATH_CALLS = {"Path", "with_name", "joinpath", "resolve", "absolute", "with_suffix"}
#: Functions that change a settings file without the guard, and why that is right.
EXEMPT: dict[str, str] = {
    "installer/stage2/web/samples.py:seed_scratch_home": (
        "copies runtime.json and a channel into the scratch home of the setup window's test render, never "
        "into the settings of the install"
    ),
}
#: Writers of settings files whose names are read at run time (the scan cannot see the name): each must call
#: the guard, or be part of the settings copy itself (run only by `ns update` and the rollback, which call
#: `first_start.settled` and handle the copy themselves).
BY_NAME_AT_RUN_TIME: dict[str, str] = {
    "ns_supervisor/backup.py:restore_backup": "guard",
    "ns_common/config_snapshot.py:restore": "copy",
    "ns_common/config_snapshot.py:set_aside": "copy",
    "ns_common/config_snapshot.py:_undo": "copy",
    "ns_supervisor/update.py:_readable_by": "copy",
}


@dataclass
class Module:
    rel: str
    name: str
    tree: ast.Module
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)  # alias -> (module, name|None)
    top: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(default_factory=dict)


def product_modules() -> dict[str, Module]:
    mods: dict[str, Module] = {}
    for pkg in PACKAGES:
        for f in sorted((ROOT / pkg).rglob("*.py")):
            rel = f.relative_to(ROOT).as_posix()
            name = rel[:-3].replace("/", ".").removesuffix(".__init__")
            m = Module(rel, name, ast.parse(f.read_text(encoding="utf-8")))
            for node in m.tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m.top[node.name] = node
            for node in ast.walk(m.tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        m.imports[a.asname or a.name.split(".")[0]] = (
                            a.name if a.asname else a.name.split(".")[0],
                            None,
                        )
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    for a in node.names:
                        m.imports[a.asname or a.name] = (node.module, a.name)
            mods[name] = m
    return mods


def functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def own_nodes(fn: ast.AST) -> Iterator[ast.AST]:
    """The nodes of `fn` without those of the functions defined inside it."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def call_name(n: ast.Call) -> tuple[str | None, str]:
    f = n.func
    if isinstance(f, ast.Name):
        return None, f.id
    if isinstance(f, ast.Attribute):
        return (f.value.id if isinstance(f.value, ast.Name) else None), f.attr
    return None, ""


def resolve(mods: dict[str, Module], m: Module, func: ast.AST) -> tuple[str, str] | None:
    """The (module, top-level function) of the product a call goes to, when it can be told from the source."""
    if isinstance(func, ast.Name):
        if func.id in m.top:
            return m.name, func.id
        imp = m.imports.get(func.id)
        return (imp[0], imp[1]) if imp and imp[1] is not None and imp[0] in mods else None
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        imp = m.imports.get(func.value.id)
        if imp is not None:
            target = imp[0] if imp[1] is None else f"{imp[0]}.{imp[1]}"
            if target in mods:
                return target, func.attr
    return None


def raw_targets(n: ast.Call) -> list[ast.AST] | None:
    """The file arguments a direct file change writes or removes (None: the call changes no file)."""
    base, name = call_name(n)
    if (base, name) in QUALIFIED:
        return [n.args[i] for i in QUALIFIED[(base, name)] if i < len(n.args)]
    if name in FILE_WRITERS and (isinstance(n.func, ast.Name) or base is not None):
        return [n.args[i] for i in FILE_WRITERS[name] if i < len(n.args)]
    if isinstance(n.func, ast.Attribute) and name in FILE_METHODS:
        return [n.func.value, *(n.args[:1] if name == "rename" else [])]
    if isinstance(n.func, ast.Attribute) and name == "replace" and len(n.args) == 1 and not n.keywords:
        return [n.func.value, n.args[0]]  # Path.replace (str.replace takes two)
    return None


def root_name(expr: ast.AST) -> str | None:
    while True:
        if isinstance(expr, ast.Name):
            return expr.id
        if isinstance(expr, ast.Attribute):
            expr = expr.value
        elif isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
            expr = expr.func.value
        elif isinstance(expr, ast.BinOp):
            expr = expr.left
        else:
            return None


def is_location(expr: ast.AST, locators: set[str]) -> bool:
    """Names, attributes, constants, `/` and path calls only: the expression of a place, not of a load."""
    for n in ast.walk(expr):
        if isinstance(n, ast.Call):
            if call_name(n)[1] not in locators | PATH_CALLS:
                return False
        elif not isinstance(
            n,
            (
                ast.Name,
                ast.Attribute,
                ast.Constant,
                ast.BinOp,
                ast.Div,
                ast.Load,
                ast.JoinedStr,
                ast.FormattedValue,
            ),
        ):
            return False
    return True


def names_settings(expr: ast.AST, locators: set[str], held: set[str]) -> bool:
    names = set(config_snapshot.FILES)
    for n in ast.walk(expr):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in names:
            return True
        if isinstance(n, ast.Attribute) and n.attr in SETTINGS_ATTRS:
            return True
        if isinstance(n, ast.Call) and call_name(n)[1] in locators:
            return True
        if isinstance(n, ast.Name) and n.id in held:
            return True
    return False


def holding_settings(fn: ast.AST, locators: set[str]) -> set[str]:
    """Local names that hold the place of a settings file (assigned from one, or a loop over their names)."""
    held: set[str] = set()
    names = set(config_snapshot.FILES)
    for _ in range(3):
        for n in own_nodes(fn):
            src: ast.AST | None = None
            targets: list[ast.AST] = []
            if isinstance(n, ast.Assign):
                src, targets = n.value, list(n.targets)
            elif isinstance(n, ast.AnnAssign) and n.value is not None:
                src, targets = n.value, [n.target]
            elif isinstance(n, ast.For):
                src, targets = n.iter, [n.target]
            if src is None:
                continue
            if isinstance(src, (ast.Tuple, ast.List, ast.Set)):
                hit = any(isinstance(e, ast.Constant) and e.value in names for e in src.elts) or any(
                    names_settings(e, locators, held) and is_location(e, locators) for e in src.elts
                )
            else:
                hit = names_settings(src, locators, held) and is_location(src, locators)
            if hit:
                for t in targets:
                    held |= {m.id for m in ast.walk(t) if isinstance(m, ast.Name)}
    return held


@dataclass
class Scan:
    flagged: dict[str, bool]  # "<file>:<function>" -> calls the guard
    guarded_writers: set[str]  # "<module>.<function>": changes the file a parameter names, guard first
    locators: set[str]


def scan_writers() -> Scan:
    mods = product_modules()
    locators: set[str] = set()  # functions that return the place of a settings file
    for _ in range(4):
        for m in mods.values():
            for fn in functions(m.tree):
                for n in own_nodes(fn):
                    if (
                        isinstance(n, ast.Return)
                        and n.value is not None
                        and is_location(n.value, locators)
                        and names_settings(n.value, locators, set())
                    ):
                        locators.add(fn.name)
    writers: dict[tuple[str, str], tuple[bool, set[str]]] = {}  # -> (guarded, the parameters it writes)

    def targets(m: Module, n: ast.Call) -> list[ast.AST]:
        raw = raw_targets(n)
        if raw is not None:
            return raw
        key = resolve(mods, m, n.func)
        if key is None or key not in writers or writers[key][0]:
            return []  # a guarded writer is not a change to look at: it takes the copy itself
        names = [
            a.arg for a in [*mods[key[0]].top[key[1]].args.posonlyargs, *mods[key[0]].top[key[1]].args.args]
        ]
        written = writers[key][1]
        out: list[ast.AST] = [a for i, a in enumerate(n.args) if i < len(names) and names[i] in written]
        return out + [k.value for k in n.keywords if k.arg in written]

    def guarded(fn: ast.AST) -> bool:
        return any(isinstance(n, ast.Call) and call_name(n)[1] in GUARD_CALLS for n in own_nodes(fn))

    for _ in range(5):
        for m in mods.values():
            for fn in m.top.values():
                params = {a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]}
                written = {
                    r
                    for n in own_nodes(fn)
                    if isinstance(n, ast.Call)
                    for t in targets(m, n)
                    if (r := root_name(t)) in params
                }
                if written:
                    writers[(m.name, fn.name)] = (guarded(fn), written)
    flagged: dict[str, bool] = {}
    for m in mods.values():
        for fn in functions(m.tree):
            held = holding_settings(fn, locators)
            if any(
                names_settings(t, locators, held)
                for n in own_nodes(fn)
                if isinstance(n, ast.Call)
                for t in targets(m, n)
            ):
                flagged[f"{m.rel}:{fn.name}"] = guarded(fn)
    return Scan(flagged, {f"{k[0]}.{k[1]}" for k, v in writers.items() if v[0]}, locators)


def test_every_writer_of_a_settings_file_calls_the_guard_first() -> None:
    """Finds every function of the product that writes, replaces or deletes a file the settings copy holds
    (by its name, a Paths attribute, or a helper that returns one) and fails when one does not call
    `first_start.guard`: such a writer could change the settings before the first start has copied them."""
    found = scan_writers()
    bypass = sorted(k for k, ok in found.flagged.items() if not ok and k not in EXEMPT)
    assert not bypass, (
        f"{bypass} change a settings file without `first_start.guard(path)` first "
        "(ns_common/first_start.py): call it before the write, or add the function to EXEMPT here with the "
        "reason it needs no copy."
    )
    stale = sorted(k for k in EXEMPT if k not in found.flagged)
    assert not stale, f"{stale} are exempt but no longer write a settings file: take them out of EXEMPT"
    # the scan is not blind: it finds the writers we know (the PR lists them)
    assert {
        "ns_common/config.py:load_install_state",
        "ns_supervisor/schedule_cfg.py:write_run_time",
        "ns_supervisor/schedule_cfg.py:clear_run_time",
        "ns_dashboard/state.py:save_dashboard_settings",
        "ns_dashboard/formats.py:_write",
        "installer/stage2/state.py:save_flags",
        "installer/stage2/devmode.py:reset_for_mode_switch",
        "installer/stage2/steps/s09_shortcuts_scheduler.py:remember_run_time",
        "installer/stage2/web/controller.py:reset_install_times",
        "installer/stage2/web/controller.py:write_step",
    } <= set(found.flagged)
    assert {
        "ns_common.config._write_text",
        "installer.stage2.answers.write_answers",
        "installer.stage2.answers.blank_secrets_in_file",
        "installer.stage2.web.answers_view.write_answers",
    } <= found.guarded_writers
    assert {"runtime_file", "channel_file", "flags_path", "schedule_file", "settings_file", "user_dir"} <= (
        found.locators
    )


def test_the_writers_that_learn_the_file_name_at_run_time_are_guarded_or_part_of_the_copy() -> None:
    mods = {m.rel: m for m in product_modules().values()}
    for where, kind in BY_NAME_AT_RUN_TIME.items():
        rel, name = where.split(":")
        fn = next(f for f in functions(mods[rel].tree) if f.name == name)
        calls = {call_name(n)[1] for n in own_nodes(fn) if isinstance(n, ast.Call)}
        if kind == "guard":
            assert calls & GUARD_CALLS, f"{where} writes settings files: it must call first_start.guard"
    # the copy's own writers run only inside `ns update` / `ns update --rollback` (which call settled)
    users = {
        m.rel
        for m in product_modules().values()
        for n in ast.walk(m.tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("restore", "set_aside")
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "config_snapshot"
    }
    # (rollback_intent puts back or finishes an interrupted rollback: it is called by the rollback and the
    # update themselves, and by the first-start check, before it looks at the copy)
    assert users == {"ns_supervisor/update.py", "ns_common/rollback_intent.py"}
    callers = {
        m.rel
        for m in product_modules().values()
        for n in ast.walk(m.tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("recover", "undo", "finish")
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "rollback_intent"
    }
    # (INSTALL.cmd's window only finishes one, never undoes: `may_undo=False`)
    assert callers == {
        "ns_supervisor/update.py",
        "ns_common/first_start.py",
        "installer/stage2/web/appcopy.py",
    }
    appcopy_src = (ROOT / "installer" / "stage2" / "web" / "appcopy.py").read_text(encoding="utf-8")
    assert 'rollback_intent.recover(dest, Path(home) / "config", may_undo=False)' in appcopy_src
    source = (ROOT / "ns_supervisor" / "update.py").read_text(encoding="utf-8")
    for entry in ("def update_app", "def rollback_app", "def git_update"):
        body = source.split(entry, 1)[1].split("\ndef ", 1)[0]
        assert "first_start.settled(paths.config)" in body, entry
