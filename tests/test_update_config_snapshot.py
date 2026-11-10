"""F-10a: `ns update --rollback` puts the settings files back together with the program.

`Runtime` (and `InstallState`, `Channel`, ...) are strict, and a newer version re-saves `runtime.json` with
every field it knows. After a rollback the older program then meets a file with a key it has never heard of
and refuses to load its own config. So the updater keeps a copy of the config files an update may rewrite
(`ns_common/config_snapshot.py`) beside the program backup, and the rollback restores both. Without a usable
copy the rollback takes out what the older version does not know (F-10c; tests/test_first_start_snapshot.py).

Everything runs on fakes: a temporary home, a fake program folder, fake side effects (no engine, no uv, no
network). The "older version" is `tests/frozen_models_0_1_14.py`, the 0.1.14 models as they were tagged.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from installer.stage2.web import appcopy
from ns_common import config_snapshot
from ns_common.config import load_channel, load_install_state, load_runtime
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor import update
from tests.frozen_models_0_1_14 import Channel as OldChannel
from tests.frozen_models_0_1_14 import InstallState as OldInstallState
from tests.frozen_models_0_1_14 import Runtime as OldRuntime
from tests.test_installer_app_copy import make_source
from tests.test_ns_update import Env, make_app, make_zip
from tests.test_ns_update_git import FakeGit

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "RENAME_PAUSE_S", 0.01)


# --------------------------------------------------------------------------------------- the fake install
def seed_config(paths: Paths, *, dev: bool = False) -> dict[str, bytes]:
    """The config folder as 0.1.14 left it (written with the 0.1.14 models); returns every file's bytes."""
    cfg = paths.config
    (cfg / "channels").mkdir(parents=True)
    (cfg / "formats").mkdir()
    runtime = OldRuntime(dev_mode=dev, profile="cpu-dev" if dev else "gpu")
    state = OldInstallState(version="0.1.14", app_path="D:\\Nightshift\\app", dev_mode=dev)
    channel = OldChannel(slug="demo", display_name="Demo", niche="true stories")
    files: dict[str, str] = {
        "runtime.json": json.dumps(runtime.model_dump(mode="json"), indent=2) + "\n",
        "install_state.json": json.dumps(state.model_dump(mode="json"), indent=2) + "\n",
        "install_state.json.bak": json.dumps(state.model_dump(mode="json")) + "\n",
        "answers.json": json.dumps({"channel": {"display_name": "Demo", "niche": "true stories"}}) + "\n",
        "wizard_flags.json": json.dumps({"tts_fallback": True, "install": "2026-11-01"}) + "\n",
        "schedule.json": json.dumps(
            {
                "run_time": "03:00",
                "task_name": "Nightshift Daily",
                "mode": "disabled" if dev else "registered",
            },
            indent=2,
        )
        + "\n",
        "dashboard.json": json.dumps({"notifications": True}) + "\n",
        "channels/demo.yaml": yaml.safe_dump(channel.model_dump(mode="json"), sort_keys=False),
        "formats/mine.yaml": yaml.safe_dump({"name": "Mine", "kind": "short"}, sort_keys=False),
        # not settings the older version can choke on: never copied, never touched
        "secrets.dpapi": "blob-of-keys",
        "ui_prefs.json": json.dumps({"owner_name": "Sam"}),
    }
    for name, text in files.items():
        (cfg / name).write_text(text, encoding="utf-8", newline="\n")
    return read_config(paths)


def read_config(paths: Paths) -> dict[str, bytes]:
    """Every file under config/ by relative name (bytes), whatever its name."""
    cfg = paths.config
    return {p.relative_to(cfg).as_posix(): p.read_bytes() for p in sorted(cfg.rglob("*")) if p.is_file()}


class NewVersion(Env):
    """The new program starting up: it re-saves runtime.json with ITS models and re-registers the task."""

    def __init__(self, paths: Paths, *, verify_ok: bool = True) -> None:
        super().__init__(verify_ok=verify_ok)
        self.paths = paths

    def verify(self, app: Path) -> bool:
        rt = Runtime.model_validate_json((self.paths.config / "runtime.json").read_text(encoding="utf-8"))
        (self.paths.config / "runtime.json").write_text(
            json.dumps(rt.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        return super().verify(app)

    def reregister(self, app: Path) -> list[str]:
        sched = self.paths.config / "schedule.json"
        if sched.exists():  # `schedule.install` writes the file again, compactly
            sched.write_text(json.dumps(json.loads(sched.read_text(encoding="utf-8"))), encoding="utf-8")
        return super().reregister(app)


def owner_changes(paths: Paths) -> None:
    """What the owner does on the new version before deciding to go back."""
    (paths.config / "dashboard.json").write_text('{"notifications": false}\n', encoding="utf-8")
    (paths.config / "channels" / "new-one.yaml").write_text("slug: new-one\n", encoding="utf-8")


def snapshot_file(app: Path) -> Path:
    return app.with_name(app.name + ".prev-config.zip")


def without_new_keys(config: dict[str, bytes]) -> dict[str, bytes]:
    """`config` as a rollback without a usable copy leaves it (F-10c): runtime.json without the keys the
    older version never had (`tiny_story`, which `NewVersion` writes), every other file as it was."""
    out = dict(config)
    data = json.loads(out["runtime.json"])
    if data.pop("tiny_story", None) is not None:
        out["runtime.json"] = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return out


def old_models_load(paths: Paths) -> None:
    """Load every config file with the 0.1.14 models (the rolled back program's) and the current ones."""
    text = (paths.config / "runtime.json").read_text(encoding="utf-8")
    OldRuntime.model_validate_json(text)
    OldInstallState.model_validate_json((paths.config / "install_state.json").read_text(encoding="utf-8"))
    for ch in sorted((paths.config / "channels").glob("*.yaml")):
        OldChannel.model_validate(yaml.safe_load(ch.read_text(encoding="utf-8")))
    load_runtime(paths)
    load_install_state(paths)
    load_channel(paths, "demo")


# --------------------------------------------------------------------------------------- the main promise
@pytest.mark.parametrize("dev", [False, True], ids=["real-install", "dev-install"])
def test_update_then_rollback_restores_every_config_file_byte_for_byte(tmp_path: Path, dev: bool) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths, dev=dev)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    after_update = read_config(paths)
    assert after_update != before  # the new program really rewrote files
    with pytest.raises(ValidationError):  # the bug: 0.1.14 cannot load what 0.1.15 saved
        OldRuntime.model_validate_json(after_update["runtime.json"])

    res = update.rollback_app(app, Env().env(), paths=paths)

    assert read_config(paths) == before  # byte-identical, including what must not move
    old_models_load(paths)  # the older program loads its own config again
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    for name in ("runtime.json", "install_state.json", "schedule.json", "channels/demo.yaml"):
        assert name in res.settings
    assert "secrets.dpapi" not in res.settings  # keys are never copied


def test_the_snapshot_is_taken_before_the_new_program_runs_and_lives_next_to_the_program_backup(
    tmp_path: Path,
) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths)
    seen: dict[str, bytes] = {}

    class Spy(NewVersion):
        def sync(self, app: Path) -> None:
            snap = snapshot_file(app)
            assert snap.is_file()  # already there when the new tree is synced
            seen.update(config_snapshot.read(snap).files)
            super().sync(app)

    res = update.update_app(make_zip(tmp_path), app, Spy(paths).env(), paths=paths)
    assert snapshot_file(app).parent == (tmp_path / "NightShift.prev").parent
    assert seen == {n: b for n, b in before.items() if n in seen}  # the files as they were
    assert {"runtime.json", "install_state.json", "channels/demo.yaml", "schedule.json"} <= set(seen)
    assert "secrets.dpapi" not in seen and "ui_prefs.json" not in seen
    assert any("rollback" in n and "runtime.json" in n for n in res.notes)


def test_without_the_snapshot_the_rollback_removes_what_the_older_version_lacks(tmp_path: Path) -> None:
    """With the copy gone (an update by an older updater) the file the new version saved is refused by the
    older models; since F-10c the rollback removes the keys they never had, and they load it."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    with pytest.raises(ValidationError):  # the control: what the new version saved, as it is
        OldRuntime.model_validate_json((paths.config / "runtime.json").read_text(encoding="utf-8"))
    snapshot_file(app).unlink()
    res = update.rollback_app(app, Env().env(), paths=paths)
    OldRuntime.model_validate_json((paths.config / "runtime.json").read_text(encoding="utf-8"))
    assert "runtime.json (tiny_story)" in res.settings


# --------------------------------------------------------------------------------------- owner changes
def test_what_the_owner_changed_between_the_update_and_the_rollback_is_lost_and_the_message_says_so(
    tmp_path: Path,
) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    owner_changes(paths)

    res = update.rollback_app(app, Env().env(), paths=paths)

    assert (paths.config / "dashboard.json").read_bytes() == before["dashboard.json"]  # the change is gone
    assert not (paths.config / "channels" / "new-one.yaml").exists()  # a channel the 0.1.14 models never saw
    assert "anything you changed in them is lost" in res.settings
    assert "dashboard.json" in res.settings.split("lost")[-1]  # named, not just "some files"
    notes = " ".join(res.notes)
    assert "channels/new-one.yaml" in notes  # set aside, and said
    assert read_config(paths) == before


def test_a_second_rollback_goes_forward_with_the_settings_the_owner_had_just_before_going_back(
    tmp_path: Path,
) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    owner_changes(paths)
    just_before = read_config(paths)

    update.rollback_app(app, Env().env(), paths=paths)
    assert read_config(paths) == before  # back to what 0.1.0 wrote
    res = update.rollback_app(app, Env().env(), paths=paths)  # forward again
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
    assert read_config(paths) == just_before  # nothing the owner did is gone for good
    assert "runtime.json" in res.settings
    assert "had them when you went back" in res.settings  # going forward is not "as the update left them"
    assert "since the update" not in res.settings
    update.rollback_app(app, Env().env(), paths=paths)  # and back once more
    assert read_config(paths) == before


def test_a_home_without_settings_files_updates_and_rolls_back_without_complaint(tmp_path: Path) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)  # no config folder at all
    res = update.update_app(make_zip(tmp_path), app, Env().env(), paths=paths)
    assert any("settings files saved" in n and "none" in n for n in res.notes)
    back = update.rollback_app(app, Env().env(), paths=paths)
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert "none were restored" in back.settings and not paths.config.exists()


def test_a_copy_of_an_empty_config_still_sets_aside_the_files_the_new_version_made(tmp_path: Path) -> None:
    """The older version had no settings files; it must not meet the new version's runtime.json either."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)  # no config folder when the update is made
    update.update_app(make_zip(tmp_path), app, Env().env(), paths=paths)
    paths.config.mkdir(parents=True)
    (paths.config / "runtime.json").write_text('{"profile": "gpu", "a_new_field": 1}\n', encoding="utf-8")
    (paths.config / "secrets.dpapi").write_text("keys", encoding="utf-8")

    res = update.rollback_app(app, Env().env(), paths=paths)

    assert not (paths.config / "runtime.json").exists()  # the 0.1.0 program never had one
    assert (paths.config / "secrets.dpapi").read_text(encoding="utf-8") == "keys"  # keys are never touched
    assert "none were restored" in res.settings
    assert any("runtime.json" in n and "set aside" in n for n in res.notes)
    update.rollback_app(app, Env().env(), paths=paths)  # and going forward gives the new file back
    assert (paths.config / "runtime.json").is_file()


# --------------------------------------------------------------------------------------- no / bad snapshot
def test_a_rollback_without_a_snapshot_swaps_the_program_as_before_and_says_the_config_was_not_restored(
    tmp_path: Path,
) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    for leftover in tmp_path.glob("NightShift.prev-*"):  # what an update by an older updater never made
        leftover.unlink()
    now = read_config(paths)

    e = Env()
    res = update.rollback_app(app, e.env(), paths=paths)

    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert e.calls == ["stop", "sync"]
    assert read_config(paths) == without_new_keys(now)  # nothing to restore from; only what 0.1.0 lacks goes
    assert "could not be restored" in res.settings and "no copy" in res.settings
    assert "ns restore" in res.settings  # and what to do if the program will not start


def test_a_damaged_snapshot_is_never_applied(tmp_path: Path) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    now = read_config(paths)
    snap = snapshot_file(app)
    snap.write_bytes(snap.read_bytes()[:-40])  # a torn copy: the end of the zip is missing

    res = update.rollback_app(app, Env().env(), paths=paths)

    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert read_config(paths) == without_new_keys(now)
    assert "could not be restored" in res.settings and "damaged" in res.settings


def test_a_snapshot_whose_files_were_edited_inside_the_zip_is_damaged(tmp_path: Path) -> None:
    import zipfile

    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    snap = snapshot_file(app)
    with zipfile.ZipFile(snap) as z:
        members = {n: z.read(n) for n in z.namelist()}
    members["config/runtime.json"] = b'{"profile": "cpu-dev"}\n'  # same name, other content
    with zipfile.ZipFile(snap, "w") as z:
        for n, data in members.items():
            z.writestr(n, data)
    with pytest.raises(config_snapshot.SnapshotError):
        config_snapshot.read(snap)


def test_a_snapshot_that_belongs_to_another_program_is_not_applied(tmp_path: Path) -> None:
    """INSTALL.cmd of an older zip (or a hand swap) can replace <app>.prev behind the copy's back."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    now = read_config(paths)
    (tmp_path / "NightShift.prev" / "VERSION").write_text("0.0.9\n", encoding="utf-8")

    res = update.rollback_app(app, Env().env(), paths=paths)

    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.0.9"
    assert read_config(paths) == without_new_keys(now)
    assert "could not be restored" in res.settings and "belongs to 0.1.0" in res.settings


# --------------------------------------------------------------------------------------- half-written copies
def test_a_snapshot_that_cannot_be_written_never_replaces_a_good_one_and_stops_the_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths)
    update.update_app(make_zip(tmp_path, "0.2.0"), app, NewVersion(paths).env(), paths=paths)
    good = snapshot_file(app).read_bytes()
    after_first = read_config(paths)
    real = os.replace

    def disk_full(src: str | Path, dst: str | Path) -> None:
        if str(dst).endswith(".prev-config.zip"):  # the crash: the publish step of the copy fails
            raise OSError(28, "No space left on device")
        real(src, dst)

    e = Env()
    with monkeypatch.context() as m:
        m.setattr(os, "replace", disk_full)
        with pytest.raises(NsError) as ei:
            update.update_app(make_zip(tmp_path, "0.3.0"), app, e.env(), paths=paths)

    assert ei.value.code == "NS-E702"
    assert "copy of your settings" in ei.value.detail and "Nothing was changed" in ei.value.detail
    assert snapshot_file(app).read_bytes() == good  # the good copy is untouched
    assert not list(tmp_path.glob("~ns*.tmp"))  # and no half file is left beside it
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"  # the program was not touched
    assert (tmp_path / "NightShift.prev" / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert not (tmp_path / "NightShift.new").exists()
    assert "sync" not in e.calls and "verify" not in e.calls
    assert read_config(paths) == after_first
    update.rollback_app(app, Env().env(), paths=paths)  # the pair still works
    assert read_config(paths) == before


def test_a_rollback_whose_restore_fails_leaves_the_original_copy_in_the_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zip is the only place the older version's files are kept: the copy of the settings as they are now
    (for going forward) may replace it only after the restore worked."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    after_update = read_config(paths)
    original = config_snapshot.read(snapshot_file(app))
    real = config_snapshot.write_bytes_atomic
    calls: list[str] = []

    def one_file_is_locked(path: Path, data: bytes) -> Path:
        calls.append(path.name)
        if path.name == "wizard_flags.json" and calls.count("wizard_flags.json") == 1:
            raise OSError(32, "in use by another program")
        return real(path, data)

    with monkeypatch.context() as m:
        m.setattr(config_snapshot, "write_bytes_atomic", one_file_is_locked)
        res = update.rollback_app(app, Env().env(), paths=paths)

    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"  # the program did go back
    assert read_config(paths) == without_new_keys(after_update)  # the restore was all or nothing (F-10c:
    # then what 0.1.0 does not know is taken out, so it can start)
    assert "could not be restored" in res.settings and "wizard_flags.json" in res.settings
    kept = config_snapshot.read(snapshot_file(app))  # still readable, still the 0.1.0 files
    assert kept.files == original.files == {n: b for n, b in before.items() if n in kept.files}
    assert (kept.version, kept.taken_by) == ("0.1.0", "update")
    assert str(snapshot_file(app)) in res.settings  # and the owner is told where it is


def test_a_config_file_that_cannot_be_read_stops_the_update_before_anything_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    real = Path.read_bytes

    def locked(self: Path) -> bytes:
        if self.name == "answers.json":
            raise PermissionError(13, "in use by another program")
        return real(self)

    with monkeypatch.context() as m:
        m.setattr(Path, "read_bytes", locked)
        with pytest.raises(NsError) as ei:
            update.update_app(make_zip(tmp_path), app, Env().env(), paths=paths)
    assert ei.value.code == "NS-E702" and "answers.json" in ei.value.detail
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert not (tmp_path / "NightShift.prev").exists() and not snapshot_file(app).exists()
    assert not (tmp_path / "NightShift.new").exists()


def test_a_crash_that_left_a_temporary_file_beside_the_copy_does_not_matter(tmp_path: Path) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths)
    (tmp_path / "~ns999-1-1.tmp").write_bytes(b"PK half a zip")
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    update.rollback_app(app, Env().env(), paths=paths)
    assert read_config(paths) == before


# --------------------------------------------------------------------------------------- a failed update
def test_a_failed_update_puts_the_settings_back_with_the_program(tmp_path: Path) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths)
    with pytest.raises(NsError) as ei:
        update.update_app(make_zip(tmp_path), app, NewVersion(paths, verify_ok=False).env(), paths=paths)
    assert ei.value.code == "NS-E702" and "ns verify --quick failed" in ei.value.detail
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert read_config(paths) == before  # verify had re-saved runtime.json: it is back
    old_models_load(paths)
    assert "settings files were put back" in ei.value.detail
    assert not snapshot_file(app).exists()  # no program backup is left, so no copy either


def test_a_failed_update_also_removes_a_settings_file_only_the_new_version_made(tmp_path: Path) -> None:
    """Same as the rollback: the older program gets the files it had, not one it has never seen."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths)

    class MakesAChannel(NewVersion):
        def verify(self, app: Path) -> bool:
            (paths.config / "channels" / "made-by-new.yaml").write_text("slug: x\n", encoding="utf-8")
            return super().verify(app)

    with pytest.raises(NsError):
        update.update_app(make_zip(tmp_path), app, MakesAChannel(paths, verify_ok=False).env(), paths=paths)
    assert not (paths.config / "channels" / "made-by-new.yaml").exists()
    assert read_config(paths) == before


def test_the_snapshot_survives_a_failed_update_whose_program_cannot_be_put_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NS-E702 says to run `ns update --rollback`; that has to bring the settings back as well."""
    monkeypatch.setattr(update, "RENAME_TRIES", 2)
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths)
    real = Path.rename
    state = {"armed": False}

    class VerifyFails(NewVersion):
        def verify(self, app: Path) -> bool:
            super().verify(app)
            state["armed"] = True  # from here on the new tree is "held" by a process
            return False

    def held(self: Path, target: Path) -> Path:
        if state["armed"] and self == app:
            raise PermissionError(32, "being used by another process")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", held)
    with pytest.raises(NsError) as ei:
        update.update_app(make_zip(tmp_path), app, VerifyFails(paths).env(), paths=paths)
    assert "ns update --rollback" in ei.value.detail
    assert read_config(paths) != before and snapshot_file(app).is_file()
    state["armed"] = False

    update.rollback_app(app, Env().env(), paths=paths)

    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert read_config(paths) == before


# --------------------------------------------------------------------------------------- the latest pair only
def test_a_second_update_before_any_rollback_keeps_only_the_latest_pair_and_says_so(tmp_path: Path) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path, "0.2.0"), app, NewVersion(paths).env(), paths=paths)
    (paths.config / "dashboard.json").write_text('{"notifications": false}\n', encoding="utf-8")
    config_at_020 = read_config(paths)  # what 0.2.0 has when 0.3.0 is installed over it

    res = update.update_app(make_zip(tmp_path, "0.3.0"), app, Env().env(), paths=paths)

    assert any("0.1.0" in n and "replaced" in n and "0.2.0 only" in n for n in res.notes)
    snap = config_snapshot.read(snapshot_file(app))
    assert (snap.version, len(list(tmp_path.glob("NightShift.prev-config*")))) == ("0.2.0", 1)
    update.rollback_app(app, Env().env(), paths=paths)
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
    assert read_config(paths) == config_at_020  # the settings 0.2.0 had, not 0.1.0's


def test_a_backup_left_by_an_older_updater_is_not_said_to_have_had_a_settings_copy(tmp_path: Path) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path, "0.2.0"), app, Env().env(), paths=paths)
    snapshot_file(app).unlink()  # <app>.prev as an update by an older updater leaves it: no copy beside it
    res = update.update_app(make_zip(tmp_path, "0.3.0"), app, Env().env(), paths=paths)
    note = next(n for n in res.notes if "earlier program backup" in n)
    assert "0.1.0" in note and "settings copy" not in note


# --------------------------------------------------------------------------------------- --git
def test_a_failed_git_update_puts_the_settings_back(tmp_path: Path) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths)
    git = FakeGit()
    with pytest.raises(NsError) as ei:
        update.git_update(
            app, NewVersion(paths, verify_ok=False).env(), paths=paths, latest_tag=lambda: "v0.2.0", git=git
        )
    assert "ns verify --quick failed" in ei.value.detail and "settings files were put back" in ei.value.detail
    assert ["checkout", "--detach", "a" * 40] in git.calls  # the old commit is back
    assert read_config(paths) == before
    assert not list(tmp_path.glob("NightShift.prev-config*"))


def test_a_git_update_leaves_the_program_backup_pair_alone(tmp_path: Path) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    kept = snapshot_file(app).read_bytes()
    res = update.git_update(app, Env().env(), paths=paths, latest_tag=lambda: "v0.3.0", git=FakeGit())
    assert snapshot_file(app).read_bytes() == kept  # a git update has no program backup to pair with
    assert "channel stable" in res.notes


# ------------------------------------------------------------------------------------ a copy that is not ours
# Review F-10a @ 896438f: a copy that reads back whole but does not belong to the program it would be
# replaced with may be the only copy of an older program's settings. Writing the settings as they are now
# over it, under that program's name, loses them and makes a later rollback "restore" the wrong files.
def rollback_with_a_locked_file(
    app: Path, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> update.UpdateResult:
    """`ns update --rollback` while another program holds wizard_flags.json: the settings restore fails."""
    real = config_snapshot.write_bytes_atomic

    def locked(path: Path, data: bytes) -> Path:
        if path.name == "wizard_flags.json":
            raise OSError(32, "in use by another program")
        return real(path, data)

    with monkeypatch.context() as m:
        m.setattr(config_snapshot, "write_bytes_atomic", locked)
        return update.rollback_app(app, Env().env(), paths=paths)


def another_programs_copy() -> bytes:
    """A whole copy that belongs to neither program on disk (an older updater or a hand swap left it)."""
    return config_snapshot.build(
        {"runtime.json": b'{"profile": "gpu"}\n'}, version="0.0.5", fingerprint="0.0.5|x|y", taken_by="update"
    )


def version_of(folder: Path) -> str:
    return (folder / "VERSION").read_text(encoding="utf-8").strip()


def test_a_failed_restore_then_two_more_rollbacks_never_give_the_older_program_the_newer_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback 1 cannot restore (a locked file): the zip still holds what 0.1.0 wrote, the live files are
    0.2.0's. Rollback 2 (forward to 0.2.0) finds a copy that is not for 0.2.0 and must leave it whole, so
    that rollback 3 (back to 0.1.0) restores 0.1.0's own files, not 0.2.0's under 0.1.0's name."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    after_update = read_config(paths)
    original = snapshot_file(app).read_bytes()

    first = rollback_with_a_locked_file(app, paths, monkeypatch)
    assert version_of(app) == "0.1.0" and "could not be restored" in first.settings
    assert read_config(paths) == without_new_keys(after_update)  # (F-10c: what 0.1.0 lacks is taken out)

    second = update.rollback_app(app, Env().env(), paths=paths)  # the owner goes forward to 0.2.0 again
    assert version_of(app) == "0.2.0"
    assert read_config(paths) == without_new_keys(after_update)  # 0.2.0's own files, untouched since
    assert snapshot_file(app).read_bytes() == original  # the only copy of 0.1.0's files is still whole
    assert "could not be restored" in second.settings
    kept = next(n for n in second.notes if str(snapshot_file(app)) in n)  # and the owner is told where
    assert "0.1.0" in kept

    third = update.rollback_app(app, Env().env(), paths=paths)  # and back to 0.1.0
    assert version_of(app) == "0.1.0"
    assert read_config(paths) == before  # what 0.1.0 wrote, not 0.2.0's files relabelled as 0.1.0's
    old_models_load(paths)
    assert "back as 0.1.0 left them" in third.settings


def test_a_rollback_never_writes_over_a_whole_copy_that_belongs_to_another_program(tmp_path: Path) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    snapshot_file(app).write_bytes(theirs := another_programs_copy())
    now = read_config(paths)

    res = update.rollback_app(app, Env().env(), paths=paths)

    assert version_of(app) == "0.1.0" and read_config(paths) == without_new_keys(now)
    assert "could not be restored" in res.settings and "belongs to 0.0.5" in res.settings
    assert snapshot_file(app).read_bytes() == theirs
    note = next(n for n in res.notes if str(snapshot_file(app)) in n)
    assert "0.0.5" in note and "left as it is" in note


def test_a_rollback_replaces_a_damaged_copy_and_says_so(tmp_path: Path) -> None:
    """Nothing in a damaged zip can be used: the settings as they are now may take its place, said aloud."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    snap = snapshot_file(app)
    snap.write_bytes(snap.read_bytes()[:-40])
    now = read_config(paths)

    res = update.rollback_app(app, Env().env(), paths=paths)

    assert read_config(paths) == without_new_keys(now) and "damaged" in res.settings
    forward = config_snapshot.read(snap)  # whole again: 0.2.0's settings, for going forward
    assert forward.version == "0.2.0" and forward.files == {n: now[n] for n in forward.files}
    assert any("damaged" in n and "replaced" in n and str(snap) in n for n in res.notes)


def test_a_copy_that_cannot_be_read_is_never_written_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zip another program holds open may be whole: neither the update nor the rollback replaces it."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    good = snapshot_file(app).read_bytes()
    real = config_snapshot.read_bytes_retry

    def held(path: Path) -> bytes:
        if path.name.endswith(".prev-config.zip"):
            raise PermissionError(32, "being used by another process")
        return real(path)

    e = Env()
    with monkeypatch.context() as m:
        m.setattr(config_snapshot, "read_bytes_retry", held)
        with pytest.raises(NsError) as ei:
            update.update_app(make_zip(tmp_path, "0.3.0"), app, e.env(), paths=paths)
        assert ei.value.code == "NS-E702" and "Nothing was changed" in ei.value.detail
        assert str(snapshot_file(app)) in ei.value.detail
        assert snapshot_file(app).read_bytes() == good and version_of(app) == "0.2.0"
        assert "sync" not in e.calls and not (tmp_path / "NightShift.new").exists()

        res = update.rollback_app(app, Env().env(), paths=paths)
        assert version_of(app) == "0.1.0" and "could not be restored" in res.settings
        assert snapshot_file(app).read_bytes() == good
        assert any(str(snapshot_file(app)) in n and "left as it is" in n for n in res.notes)


def test_an_update_after_a_failed_restore_keeps_the_copy_the_older_program_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After rollback 1 failed, 0.1.0 runs on 0.2.0's files. An update from there must not save those as
    0.1.0's copy over the one 0.1.0 really wrote: the copy already belongs to the program going to .prev."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    before = seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    original = snapshot_file(app).read_bytes()
    rollback_with_a_locked_file(app, paths, monkeypatch)

    res = update.update_app(make_zip(tmp_path, "0.3.0"), app, NewVersion(paths).env(), paths=paths)

    assert version_of(app) == "0.3.0" and version_of(tmp_path / "NightShift.prev") == "0.1.0"
    assert snapshot_file(app).read_bytes() == original
    assert any("0.1.0" in n and "kept" in n and str(snapshot_file(app)) in n for n in res.notes)
    assert not any("settings files saved" in n for n in res.notes)
    assert not any("its settings copy were" in n for n in res.notes)  # 0.2.0 had no copy to lose

    back = update.rollback_app(app, Env().env(), paths=paths)
    assert version_of(app) == "0.1.0" and "back as 0.1.0 left them" in back.settings
    assert read_config(paths) == before
    old_models_load(paths)


def test_a_failed_update_after_a_failed_restore_leaves_the_older_programs_copy_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    original = snapshot_file(app).read_bytes()
    rollback_with_a_locked_file(app, paths, monkeypatch)
    live = read_config(paths)

    fails = NewVersion(paths, verify_ok=False)
    with pytest.raises(NsError) as ei:
        update.update_app(make_zip(tmp_path, "0.3.0"), app, fails.env(), paths=paths)

    assert "settings files were put back" in ei.value.detail
    assert version_of(app) == "0.1.0" and read_config(paths) == live  # as they were before this update
    assert snapshot_file(app).read_bytes() == original  # not the update's to discard


def test_an_update_goes_on_and_never_writes_over_a_whole_copy_of_a_program_that_is_not_installed(
    tmp_path: Path,
) -> None:
    """Review F-10a @ b800487: refusing here blocked every later update for good (the owner deleted
    NightShift.prev and the copy was nobody's). The update goes on, leaves the zip where it is, and says
    whose it is, where, and that no copy is kept for this program backup."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    snapshot_file(app).write_bytes(theirs := another_programs_copy())

    res = update.update_app(make_zip(tmp_path, "0.3.0"), app, NewVersion(paths).env(), paths=paths)

    assert version_of(app) == "0.3.0" and version_of(tmp_path / "NightShift.prev") == "0.2.0"
    assert snapshot_file(app).read_bytes() == theirs
    note = next(n for n in res.notes if str(snapshot_file(app)) in n)
    assert "0.0.5" in note and "left as it is" in note and "No settings copy is kept" in note
    assert not any("settings files saved" in n for n in res.notes)

    back = update.rollback_app(
        app, Env().env(), paths=paths
    )  # the programs swap; the settings are not touched
    assert version_of(app) == "0.2.0" and "could not be restored" in back.settings
    assert snapshot_file(app).read_bytes() == theirs


def test_an_update_after_the_owner_deleted_the_program_backup_is_not_blocked(tmp_path: Path) -> None:
    """The reproduction from the review: update, delete NightShift.prev (a disk clean-up), update again."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    shutil.rmtree(tmp_path / "NightShift.prev")
    left = snapshot_file(app).read_bytes()

    res = update.update_app(make_zip(tmp_path, "0.3.0"), app, NewVersion(paths).env(), paths=paths)

    assert version_of(app) == "0.3.0" and version_of(tmp_path / "NightShift.prev") == "0.2.0"
    assert snapshot_file(app).read_bytes() == left  # the copy of 0.1.0 is still whole, still where it was
    assert any("0.1.0" in n and str(snapshot_file(app)) in n and "left as it is" in n for n in res.notes)


def test_a_failed_update_beside_a_whole_copy_of_another_program_puts_the_settings_back_and_keeps_the_copy(
    tmp_path: Path,
) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    snapshot_file(app).write_bytes(theirs := another_programs_copy())
    now = read_config(paths)

    with pytest.raises(NsError) as ei:
        update.update_app(
            make_zip(tmp_path, "0.3.0"), app, NewVersion(paths, verify_ok=False).env(), paths=paths
        )

    assert "settings files were put back" in ei.value.detail
    assert version_of(app) == "0.2.0" and read_config(paths) == now
    assert snapshot_file(app).read_bytes() == theirs


def swap_programs_by_hand(app: Path) -> None:
    """What an older updater or a hand swap does: the programs trade places and the zip is not read."""
    prev, tmp = app.with_name(app.name + ".prev"), app.with_name(app.name + ".swap")
    os.rename(app, tmp)
    os.rename(prev, app)
    os.rename(tmp, prev)


def test_an_update_after_a_normal_rollback_and_a_program_swap_saves_the_settings_the_owner_has_now(
    tmp_path: Path,
) -> None:
    """Review F-10a @ b800487: a zip that carries the fingerprint of the program now in `app` is not proof
    that a restore failed (a normal rollback leaves one: the forward copy). The old code kept it and the next
    rollback gave back a stale copy: the owner lost a change, and the result said so."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    update.rollback_app(app, Env().env(), paths=paths)  # 0.1.0 runs; the zip is the forward copy of 0.2.0
    assert config_snapshot.read(snapshot_file(app)).version == "0.2.0"
    swap_programs_by_hand(app)  # 0.2.0 runs again, the zip untouched
    assert version_of(app) == "0.2.0"
    (paths.config / "dashboard.json").write_bytes(b'{"notifications": "owner-edit"}\n')

    res = update.update_app(make_zip(tmp_path, "0.3.0"), app, NewVersion(paths).env(), paths=paths)

    assert any("settings files saved" in n for n in res.notes)
    assert not any("is kept in" in n for n in res.notes)
    saved = config_snapshot.read(snapshot_file(app))
    assert saved.version == "0.2.0" and saved.files["dashboard.json"] == b'{"notifications": "owner-edit"}\n'
    back = update.rollback_app(app, Env().env(), paths=paths)
    assert version_of(app) == "0.2.0"
    assert (paths.config / "dashboard.json").read_bytes() == b'{"notifications": "owner-edit"}\n'
    assert "back as 0.2.0 left them" in back.settings or "had them when you went back" in back.settings


def test_a_rollback_after_a_normal_rollback_and_a_program_swap_saves_the_settings_for_going_forward(
    tmp_path: Path,
) -> None:
    """Same chain, ending in a rollback instead of an update: the stale copy of the leaving program is
    replaced by the live files, which are that program's own."""
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    update.rollback_app(app, Env().env(), paths=paths)
    swap_programs_by_hand(app)
    (paths.config / "dashboard.json").write_bytes(b'{"notifications": "owner-edit"}\n')

    update.rollback_app(app, Env().env(), paths=paths)  # 0.2.0 -> 0.1.0: the zip is 0.2.0's, not 0.1.0's

    forward = config_snapshot.read(snapshot_file(app))
    assert (
        forward.version == "0.2.0" and forward.files["dashboard.json"] == b'{"notifications": "owner-edit"}\n'
    )


def test_a_failed_restore_is_recorded_beside_the_zip_and_a_new_copy_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    zip_path = snapshot_file(app)
    marker = zip_path.with_name(zip_path.name + ".unrestored")
    assert not marker.exists()

    rollback_with_a_locked_file(app, paths, monkeypatch)
    assert marker.is_file() and config_snapshot.is_unrestored(zip_path)

    update.update_app(
        make_zip(tmp_path, "0.3.0"), app, NewVersion(paths).env(), paths=paths
    )  # kept: still marked
    assert config_snapshot.is_unrestored(zip_path)

    update.update_app(make_zip(tmp_path, "0.4.0"), app, NewVersion(paths).env(), paths=paths)  # replaced
    assert not marker.exists() and not config_snapshot.is_unrestored(zip_path)
    assert config_snapshot.read(zip_path).version == "0.3.0"


def test_a_rollback_that_cannot_read_the_live_settings_records_that_nothing_was_put_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    real = config_snapshot.capture

    def unreadable(config: Path, only: object = None) -> dict[str, bytes]:
        raise config_snapshot.SnapshotError("could not read runtime.json: in use")

    with monkeypatch.context() as m:
        m.setattr(config_snapshot, "capture", unreadable)
        res = update.rollback_app(app, Env().env(), paths=paths)
    assert real is config_snapshot.capture and "could not be restored" in res.settings
    assert config_snapshot.is_unrestored(snapshot_file(app))


def test_the_record_of_a_failed_restore_belongs_to_one_zip_only(tmp_path: Path) -> None:
    zip_path = tmp_path / "NightShift.prev-config.zip"
    one = config_snapshot.save(
        zip_path, {"runtime.json": b"{}"}, version="0.1.0", fingerprint="0.1.0|a|b", taken_by="update"
    )
    assert one.version == "0.1.0" and not config_snapshot.is_unrestored(zip_path)
    config_snapshot.mark_unrestored(zip_path)
    assert config_snapshot.is_unrestored(zip_path)
    zip_path.write_bytes(zip_path.read_bytes() + b"\0")  # not the copy that was marked
    assert not config_snapshot.is_unrestored(zip_path)
    config_snapshot.mark_unrestored(zip_path)
    config_snapshot.discard(zip_path)
    assert not zip_path.exists() and not zip_path.with_name(zip_path.name + ".unrestored").exists()


def test_an_update_replaces_a_damaged_copy_and_says_so(tmp_path: Path) -> None:
    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    snap = snapshot_file(app)
    snap.write_bytes(snap.read_bytes()[:-40])

    res = update.update_app(make_zip(tmp_path, "0.3.0"), app, Env().env(), paths=paths)

    assert config_snapshot.read(snap).version == "0.2.0"
    assert any("damaged" in n and "replaced" in n and str(snap) in n for n in res.notes)


def test_an_install_cmd_update_after_a_failed_restore_keeps_the_older_programs_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, paths = tmp_path / "home", Paths(tmp_path / "home")
    appcopy.install_app(make_source(tmp_path / "zip-014", "0.1.4"), home)
    before = seed_config(paths)
    appcopy.install_app(make_source(tmp_path / "zip-015", "0.1.5"), home)
    rt = Runtime.model_validate_json((paths.config / "runtime.json").read_text(encoding="utf-8"))
    (paths.config / "runtime.json").write_text(json.dumps(rt.model_dump(mode="json")), encoding="utf-8")
    original = (home / "app.prev-config.zip").read_bytes()
    rollback_with_a_locked_file(home / "app", paths, monkeypatch)  # 0.1.4 now runs on 0.1.5's files
    said: list[str] = []

    appcopy.install_app(make_source(tmp_path / "zip-016", "0.1.6"), home, say=said.append)

    assert appcopy.installed_version(home) == "0.1.6" and version_of(home / "app.prev") == "0.1.4"
    assert (home / "app.prev-config.zip").read_bytes() == original
    assert any("0.1.4" in s and "kept" in s for s in said)
    update.rollback_app(home / "app", Env().env(), paths=paths)
    assert version_of(home / "app") == "0.1.4" and read_config(paths) == before


def test_an_install_cmd_update_goes_on_and_never_writes_over_a_whole_copy_of_a_program_that_is_not_installed(
    tmp_path: Path,
) -> None:
    home, paths = tmp_path / "home", Paths(tmp_path / "home")
    appcopy.install_app(make_source(tmp_path / "zip-014", "0.1.4"), home)
    seed_config(paths)
    appcopy.install_app(make_source(tmp_path / "zip-015", "0.1.5"), home)
    zip_path = home / "app.prev-config.zip"
    zip_path.write_bytes(theirs := another_programs_copy())
    said: list[str] = []

    appcopy.install_app(make_source(tmp_path / "zip-016", "0.1.6"), home, say=said.append)

    assert zip_path.read_bytes() == theirs
    assert appcopy.installed_version(home) == "0.1.6" and version_of(home / "app.prev") == "0.1.5"
    assert not (home / "app.new").exists()
    assert any("0.0.5" in s and str(zip_path) in s and "left as it is" in s for s in said)


def test_an_install_cmd_update_after_a_normal_rollback_and_a_program_swap_saves_the_current_settings(
    tmp_path: Path,
) -> None:
    home, paths = tmp_path / "home", Paths(tmp_path / "home")
    appcopy.install_app(make_source(tmp_path / "zip-014", "0.1.4"), home)
    seed_config(paths)
    appcopy.install_app(make_source(tmp_path / "zip-015", "0.1.5"), home)
    update.rollback_app(home / "app", Env().env(), paths=paths)  # 0.1.4 runs; the zip is 0.1.5's forward copy
    swap_programs_by_hand(home / "app")  # 0.1.5 runs again
    (paths.config / "dashboard.json").write_bytes(b'{"notifications": "owner-edit"}\n')

    appcopy.install_app(make_source(tmp_path / "zip-016", "0.1.6"), home)

    saved = config_snapshot.read(home / "app.prev-config.zip")
    assert saved.version == "0.1.5" and saved.files["dashboard.json"] == b'{"notifications": "owner-edit"}\n'


# --------------------------------------------------------------------------------------- the CLI line
def test_the_rollback_result_the_dashboard_shows_carries_the_settings_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor import cli, control

    paths, app = Paths(tmp_path / "ns"), make_app(tmp_path)
    seed_config(paths)
    update.update_app(make_zip(tmp_path), app, NewVersion(paths).env(), paths=paths)
    monkeypatch.setattr(control, "app_path", lambda p: app)
    monkeypatch.setattr(cli, "_stop_engine", lambda p: None)
    monkeypatch.setattr(cli, "_sync", lambda p, a: None)
    out = cli._apply_update(paths, None, True, False)
    assert out.message.startswith("Now on 0.1.0 (was 0.2.0).")
    assert "runtime.json" in out.message and "lost" in out.message


# --------------------------------------------------------------------------------------- the installer swap
def test_an_install_cmd_update_snapshots_the_settings_and_the_updater_can_roll_it_back(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    paths = Paths(home)
    appcopy.install_app(make_source(tmp_path / "zip-014", "0.1.4"), home)
    before = seed_config(paths)

    appcopy.install_app(make_source(tmp_path / "zip-015", "0.1.5"), home)
    # (what the setup steps do next: the new program re-saves runtime.json)
    rt = Runtime.model_validate_json((paths.config / "runtime.json").read_text(encoding="utf-8"))
    (paths.config / "runtime.json").write_text(json.dumps(rt.model_dump(mode="json")), encoding="utf-8")
    assert read_config(paths) != before

    res = update.rollback_app(home / "app", Env().env(), paths=paths)
    assert (home / "app" / "VERSION").read_text(encoding="utf-8").strip() == "0.1.4"
    assert read_config(paths) == before
    assert "runtime.json" in res.settings


def test_an_install_cmd_update_that_cannot_save_the_settings_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, paths = tmp_path / "home", Paths(tmp_path / "home")
    appcopy.install_app(make_source(tmp_path / "zip-014", "0.1.4"), home)
    seed_config(paths)
    real = os.replace

    def disk_full(src: str | Path, dst: str | Path) -> None:
        if str(dst).endswith(".prev-config.zip"):
            raise OSError(28, "No space left on device")
        real(src, dst)

    with monkeypatch.context() as m:
        m.setattr(os, "replace", disk_full)
        with pytest.raises(appcopy.AppCopyError) as ei:
            appcopy.install_app(make_source(tmp_path / "zip-015", "0.1.5"), home)
    assert "copy of your settings" in ei.value.detail and "was not replaced" in ei.value.detail
    assert appcopy.installed_version(home) == "0.1.4"
    assert not (home / "app.new").exists() and not (home / "app.prev").exists()


def test_an_install_cmd_update_replaces_the_snapshot_of_an_earlier_update(tmp_path: Path) -> None:
    """Two programs, one slot: the copy always belongs to the program in <app>.prev."""
    home, paths = tmp_path / "home", Paths(tmp_path / "home")
    appcopy.install_app(make_source(tmp_path / "zip-014", "0.1.4"), home)
    seed_config(paths)
    appcopy.install_app(make_source(tmp_path / "zip-015", "0.1.5"), home)
    appcopy.install_app(make_source(tmp_path / "zip-016", "0.1.6"), home)
    snap = config_snapshot.read(home / "app.prev-config.zip")
    assert snap.version == "0.1.5" and snap.taken_by == "installer"
    assert snap.fingerprint == update.app_fingerprint(home / "app.prev")


def test_an_install_cmd_update_whose_swap_fails_leaves_no_copy_for_a_program_backup_that_is_not_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, paths = tmp_path / "home", Paths(tmp_path / "home")
    appcopy.install_app(make_source(tmp_path / "zip-014", "0.1.4"), home)
    before = seed_config(paths)
    real = appcopy._rename
    calls: list[str] = []

    def second_rename_fails(src: Path, dst: Path, sleep: object) -> None:
        calls.append(dst.name)
        if src.name == "app.new":
            raise PermissionError(32, "being used by another process")
        real(src, dst, sleep)  # type: ignore[arg-type]

    with monkeypatch.context() as m:
        m.setattr(appcopy, "_rename", second_rename_fails)
        with pytest.raises(appcopy.AppCopyError):
            appcopy.install_app(make_source(tmp_path / "zip-015", "0.1.5"), home)
    # the old folder went aside, the new one could not follow, the old one came back
    assert calls == ["app.prev", "app", "app"]
    assert appcopy.installed_version(home) == "0.1.4" and not (home / "app.new").exists()
    assert not (home / "app.prev-config.zip").exists()  # nothing in <app>.prev: no copy waits for it
    assert read_config(paths) == before


def test_an_install_cmd_update_through_the_setup_window_keeps_the_state_file_the_old_program_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window records the new version in install_state.json the moment it opens, long before Continue
    copies the program. The copy for `ns update --rollback` must hold the file as 0.1.4 wrote it, so that
    after the rollback the older version has exactly its own state (the route INSTALL.cmd takes)."""
    from tests.test_installer_app_copy import app_setup, fake_zip_folder
    from tests.test_installer_web_controller import finish_script, finished_install

    zip_old = fake_zip_folder(tmp_path / "zip-old", "0.1.4")
    finished_install(app_setup(tmp_path, monkeypatch, zip_old), zip_old)
    home, paths = tmp_path / "home", Paths(tmp_path / "home")
    before = read_config(paths)
    assert json.loads(before["install_state.json"])["version"] == "0.1.4"

    s = app_setup(tmp_path, monkeypatch, fake_zip_folder(tmp_path / "zip-new", "0.1.5"), finish_script)
    opened = json.loads((paths.config / "install_state.json").read_text(encoding="utf-8"))
    assert opened["version"] == "0.1.5"  # the window has rewritten it already (the cause of the defect)
    assert s.ctl.do_action("start")[0] == 200
    s.until(lambda st: st["run"] == "finished", what="the update to finish")
    assert appcopy.installed_version(home) == "0.1.5"

    snap = config_snapshot.read(home / "app.prev-config.zip")
    assert snap.files["install_state.json"] == before["install_state.json"]
    assert "install_state.json.bak" not in snap.files  # the 0.1.4 install had none; the window made it

    res = update.rollback_app(home / "app", Env().env(), paths=paths)

    assert (home / "app" / "VERSION").read_text(encoding="utf-8").strip() == "0.1.4"
    assert (paths.config / "install_state.json").read_bytes() == before["install_state.json"]
    assert not (paths.config / "install_state.json.bak").exists()
    assert "install_state.json" in res.settings


# --------------------------------------------------------------------------------------- uninstall
def test_the_uninstallers_remove_the_settings_copy_with_the_program_backup(tmp_path: Path) -> None:
    """The copy holds the owner's settings: an uninstall must not leave it behind beside the backup."""
    from installer.stage2.web import uninstall

    script = uninstall.deferred_script(tmp_path / "ns", [1])
    assert str(tmp_path / "ns" / "app.prev-config.zip").replace("'", "''") in script
    # (and the marker a failed restore leaves beside it)
    assert str(tmp_path / "ns" / "app.prev-config.zip.unrestored").replace("'", "''") in script
    ps1 = (ROOT / "installer" / "uninstall.ps1").read_text(encoding="utf-8")
    assert "'app.prev-config.zip'" in ps1 and "'app.prev-config.zip.unrestored'" in ps1
    # F-10c: the copy's lock file and the record of a rollback in progress go too
    for name in ("app.prev-config.zip.lock", "app.rollback-pending.json"):
        assert str(tmp_path / "ns" / name).replace("'", "''") in script and f"'{name}'" in ps1


# --------------------------------------------------------------------------------------- the file list
CONFIG_NAME = re.compile(r"""config["']?\s*/\s*["']([A-Za-z0-9_.-]+\.(?:json|yaml|dpapi|bak))["']""")


def written_config_names() -> set[str]:
    """File names under <home>/config that the product's code mentions (`... config / "x.json"`)."""
    found: set[str] = set()
    for folder in ("ns_common", "ns_supervisor", "ns_dashboard", "ns_studio", "ns_tts", "installer"):
        for src in (ROOT / folder).rglob("*.py"):
            found |= set(CONFIG_NAME.findall(src.read_text(encoding="utf-8", errors="replace")))
    return found


def test_every_config_file_the_code_names_is_either_snapshotted_or_left_alone_for_a_stated_reason() -> None:
    """A new file under config/ must be decided here, in the release that adds it: that is the whole point."""
    known = set(config_snapshot.FILES) | set(config_snapshot.LEFT_ALONE)
    unknown = sorted(written_config_names() - known)
    assert not unknown, (
        f"{unknown} are named in the code but not in ns_common/config_snapshot.py: add each to FILES "
        "(a strict model or a file the update rewrites: the rollback must put it back) or to LEFT_ALONE "
        "(with the reason the older version can read it as it is)."
    )
    assert not set(config_snapshot.FILES) & set(config_snapshot.LEFT_ALONE)
    for name, why in {**config_snapshot.FILES, **config_snapshot.LEFT_ALONE}.items():
        assert len(why) > 20, f"{name}: say why"
    stale = sorted(
        n for n in config_snapshot.FILES if not n.endswith(".bak") and n not in written_config_names()
    )
    assert not stale, f"{stale} are listed in FILES but no code writes them any more"


def test_the_snapshot_covers_what_the_older_models_are_strict_about() -> None:
    assert {"runtime.json", "install_state.json", "answers.json", "wizard_flags.json"} <= set(
        config_snapshot.FILES
    )
    assert {"schedule.json", "dashboard.json"} <= set(config_snapshot.FILES)  # the update's own re-register
    assert set(config_snapshot.TREES) == {"channels", "formats"}
    assert "secrets.dpapi" in config_snapshot.LEFT_ALONE  # keys are never duplicated


def test_names_in_a_snapshot_cannot_leave_the_config_folder() -> None:
    for bad in (
        "../x.json",
        "channels/../../x.yaml",
        "C:/x.json",
        "channels\\x.yaml",
        "runtime.json/x",
        "x.txt",
    ):
        assert not config_snapshot.valid_name(bad), bad
    for good in ("runtime.json", "channels/demo.yaml", "formats/mine.yaml", "install_state.json.bak"):
        assert config_snapshot.valid_name(good), good


def test_restore_undoes_a_half_done_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One file that cannot be written puts every file already written back to what it was."""
    paths = Paths(tmp_path / "ns")
    seed_config(paths)
    snap_files = config_snapshot.capture(paths.config)
    snap = config_snapshot.parse(
        config_snapshot.build(snap_files, version="0.1.0", fingerprint="f", taken_by="update")
    )
    (paths.config / "runtime.json").write_text("{}", encoding="utf-8")
    (paths.config / "schedule.json").write_text("{}", encoding="utf-8")
    (paths.config / "channels" / "extra.yaml").write_text("slug: extra\n", encoding="utf-8")
    live = config_snapshot.capture(paths.config)
    real = config_snapshot.write_bytes_atomic
    calls: list[str] = []

    def failing(path: Path, data: bytes) -> Path:
        calls.append(path.name)
        if path.name == "wizard_flags.json" and calls.count("wizard_flags.json") == 1:
            raise OSError(5, "access denied")
        return real(path, data)

    with monkeypatch.context() as m:
        m.setattr(config_snapshot, "write_bytes_atomic", failing)
        with pytest.raises(config_snapshot.SnapshotError):
            config_snapshot.restore(paths.config, snap.files, live=live, remove_extra=True)
    assert config_snapshot.capture(paths.config) == live  # every file is as it was before the attempt
