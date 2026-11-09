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


def test_a_first_start_that_cannot_save_the_copy_goes_on_and_never_takes_a_late_one(inst: Install) -> None:
    seed(inst.paths)
    inst.old_updater("0.1.15")
    inst.copy.mkdir()  # the copy's place cannot be written (a stand-in for a full or locked disk)

    inst.start()
    owner_uses_the_new_version(inst.paths)  # the write is not stopped

    assert json.loads((inst.paths.config / "runtime.json").read_text(encoding="utf-8"))["tiny_story"] is True
    failed = [r for r in log_lines(inst.paths) if r["event"] == "first_start_copy_failed"]
    assert len(failed) == 1 and failed[0]["level"] == "WARN"
    assert "ns update --rollback will have no copy" in failed[0]["msg"].replace("`", "")
    assert first_start.read_state(inst.app)["outcome"] == "no copy"

    inst.copy.rmdir()  # writable again: a later start must not copy what this version has already changed
    inst.start()
    save_runtime(inst.paths, load_runtime(inst.paths))
    assert not inst.copy.exists()

    res = inst.rollback()
    assert "no copy" in res.settings and "runtime.json (tiny_story)" in res.settings
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
    assert users == {"ns_supervisor/update.py"}
    source = (ROOT / "ns_supervisor" / "update.py").read_text(encoding="utf-8")
    for entry in ("def update_app", "def rollback_app", "def git_update"):
        body = source.split(entry, 1)[1].split("\ndef ", 1)[0]
        assert "first_start.settled(paths.config)" in body, entry
