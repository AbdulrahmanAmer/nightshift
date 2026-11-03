import hashlib
import os
import zipfile
from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import update


def make_app(tmp_path: Path, version: str = "0.1.0") -> Path:
    app = tmp_path / "NightShift"
    (app / ".git").mkdir(parents=True)
    (app / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    (app / "config").mkdir()
    (app / "config" / "c.json").write_text('{"keep": true}', encoding="utf-8")
    (app / "ns_common").mkdir()
    (app / "ns_common" / "x.py").write_text("OLD", encoding="utf-8")
    (app / "VERSION").write_text(version + "\n", encoding="utf-8")
    (app / "answers.json").write_text('{"a": 1}', encoding="utf-8")
    (app / "local_notes.txt").write_text("mine", encoding="utf-8")
    return app


def make_zip(
    tmp_path: Path,
    version: str = "0.2.0",
    *,
    prefix: str = "",
    sidecar: bool = True,
    extra: dict[str, str] | None = None,
    with_version: bool = True,
) -> Path:
    z = tmp_path / f"Nightshift-{version}.zip"
    with zipfile.ZipFile(z, "w") as zf:
        if with_version:
            zf.writestr(prefix + "VERSION", version + "\n")
        zf.writestr(prefix + "ns_common/x.py", "NEW")
        zf.writestr(prefix + "new_only.txt", "fresh")
        for name, text in (extra or {}).items():
            zf.writestr(name, text)
    if sidecar:
        digest = hashlib.sha256(z.read_bytes()).hexdigest()
        z.with_name(z.name + ".sha256").write_text(f"{digest}  {z.name}\n", encoding="utf-8")
    return z


class Env:
    def __init__(self, *, verify_ok: bool = True, sync_error: str | None = None) -> None:
        self.calls: list[str] = []
        self.verify_ok = verify_ok
        self.sync_error = sync_error

    def stop(self) -> None:
        self.calls.append("stop")

    def sync(self, app: Path) -> None:
        self.calls.append("sync")
        if self.sync_error and self.calls.count("sync") == 1:
            raise NsError("NS-E702", self.sync_error)

    def verify(self, app: Path) -> bool:
        self.calls.append("verify")
        return self.verify_ok

    def reregister(self, app: Path) -> list[str]:
        self.calls.append("reregister")
        return ["task re-registered"]

    def env(self) -> update.UpdateEnv:
        return update.UpdateEnv(
            self.stop, self.sync, self.verify, self.reregister, untracked=lambda app: ["local_notes.txt"]
        )


def test_successful_update_swaps_and_keeps_user_data(tmp_path: Path) -> None:
    app, z = make_app(tmp_path), make_zip(tmp_path)
    e = Env()
    res = update.update_app(z, app, e.env(), paths=Paths(tmp_path / "ns"))
    assert (res.old_version, res.new_version) == ("0.1.0", "0.2.0")
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
    assert (app / "ns_common" / "x.py").read_text(encoding="utf-8") == "NEW"
    assert (app / "new_only.txt").exists()
    assert (app / ".git" / "HEAD").exists()
    assert (app / "config" / "c.json").read_text(encoding="utf-8") == '{"keep": true}'
    assert (app / "answers.json").exists() and (app / "local_notes.txt").read_text(encoding="utf-8") == "mine"
    prev = tmp_path / "NightShift.prev"
    assert (prev / "ns_common" / "x.py").read_text(encoding="utf-8") == "OLD"
    assert not (tmp_path / "NightShift.new").exists()
    assert e.calls == ["stop", "sync", "verify", "reregister"]
    assert "task re-registered" in res.notes


def test_zip_with_single_top_level_folder(tmp_path: Path) -> None:
    app, z = make_app(tmp_path), make_zip(tmp_path, prefix="Nightshift-0.2.0/")
    update.update_app(z, app, Env().env(), paths=Paths(tmp_path / "ns"))
    assert (app / "ns_common" / "x.py").read_text(encoding="utf-8") == "NEW"
    assert not (app / "Nightshift-0.2.0").exists()


def test_bad_digest_is_e701_and_changes_nothing(tmp_path: Path) -> None:
    app, z = make_app(tmp_path), make_zip(tmp_path)
    z.with_name(z.name + ".sha256").write_text("0" * 64 + "  x\n", encoding="utf-8")
    e = Env()
    with pytest.raises(NsError) as ei:
        update.update_app(z, app, e.env(), paths=Paths(tmp_path / "ns"))
    assert ei.value.code == "NS-E701" and "mismatch" in ei.value.detail
    assert e.calls == []
    assert (app / "ns_common" / "x.py").read_text(encoding="utf-8") == "OLD"
    assert not (tmp_path / "NightShift.prev").exists()


def test_missing_sidecar_and_missing_version_are_e701(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    no_side = make_zip(tmp_path, sidecar=False)
    with pytest.raises(NsError) as e1:
        update.update_app(no_side, app, Env().env(), paths=Paths(tmp_path / "ns"))
    assert e1.value.code == "NS-E701" and "sha256" in e1.value.detail
    sub = tmp_path / "x"
    sub.mkdir()
    no_ver = make_zip(sub, with_version=False)
    with pytest.raises(NsError) as e2:
        update.update_app(no_ver, app, Env().env(), paths=Paths(tmp_path / "ns"))
    assert e2.value.code == "NS-E701" and "VERSION" in e2.value.detail


def test_zip_slip_is_refused(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    z = make_zip(tmp_path, extra={"../evil.txt": "x", "..\\evil2.txt": "x"})
    with pytest.raises(NsError) as ei:
        update.update_app(z, app, Env().env(), paths=Paths(tmp_path / "ns"))
    assert ei.value.code == "NS-E701"
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "evil2.txt").exists()
    assert (app / "ns_common" / "x.py").read_text(encoding="utf-8") == "OLD"
    assert not (tmp_path / "NightShift.new").exists()


def test_failed_verify_rolls_back_with_e702(tmp_path: Path) -> None:
    app, z = make_app(tmp_path), make_zip(tmp_path)
    e = Env(verify_ok=False)
    with pytest.raises(NsError) as ei:
        update.update_app(z, app, e.env(), paths=Paths(tmp_path / "ns"))
    assert ei.value.code == "NS-E702"
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert (app / "ns_common" / "x.py").read_text(encoding="utf-8") == "OLD"
    assert not (tmp_path / "NightShift.prev").exists() and not (tmp_path / "NightShift.failed").exists()
    assert e.calls == ["stop", "sync", "verify", "sync"]  # second sync re-installs the old tree


def test_failed_sync_rolls_back(tmp_path: Path) -> None:
    app, z = make_app(tmp_path), make_zip(tmp_path)
    e = Env(sync_error="uv exploded")
    with pytest.raises(NsError) as ei:
        update.update_app(z, app, e.env(), paths=Paths(tmp_path / "ns"))
    assert ei.value.code == "NS-E702" and "uv exploded" in ei.value.detail
    assert (app / "ns_common" / "x.py").read_text(encoding="utf-8") == "OLD"


def test_rollback_swaps_back_and_forth(tmp_path: Path) -> None:
    app, z = make_app(tmp_path), make_zip(tmp_path)
    paths = Paths(tmp_path / "ns")
    update.update_app(z, app, Env().env(), paths=paths)
    e = Env()
    res = update.rollback_app(app, e.env(), paths=paths)
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert (tmp_path / "NightShift.prev" / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
    assert res.new_version == "0.1.0" and e.calls == ["stop", "sync"]
    update.rollback_app(app, Env().env(), paths=paths)  # roll forward again
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"


def test_rollback_without_previous_version_is_e702(tmp_path: Path) -> None:
    with pytest.raises(NsError) as ei:
        update.rollback_app(make_app(tmp_path), Env().env(), paths=Paths(tmp_path / "ns"))
    assert ei.value.code == "NS-E702"


def test_second_update_replaces_the_old_prev(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    paths = Paths(tmp_path / "ns")
    update.update_app(make_zip(tmp_path, "0.2.0"), app, Env().env(), paths=paths)
    update.update_app(make_zip(tmp_path, "0.3.0"), app, Env().env(), paths=paths)
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.3.0"
    assert (tmp_path / "NightShift.prev" / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"


def test_resync_venvs_builds_uv_commands_for_existing_venvs(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    for venv in ("studio", "tts"):
        py = paths.venvs / venv / "Scripts" / "python.exe"
        py.parent.mkdir(parents=True)
        py.write_text("", encoding="utf-8")
    cmds: list[list[str]] = []

    def run(cmd: list[str]) -> tuple[int, str]:
        cmds.append(cmd)
        return 0, ""

    app = tmp_path / "app"
    done = update.resync_venvs(app, paths, uv="uv", run=run)
    assert done == ["studio", "tts"]
    assert cmds[0][:5] == ["uv", "pip", "install", "-e", f"{app}[studio,supervisor]"]
    assert "--no-deps" not in cmds[0]
    assert cmds[1][4] == f"{app}[tts]" and cmds[1][-1] == "--no-deps"


def test_resync_failure_and_missing_uv_are_e702(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "ns")
    py = paths.venvs / "studio" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    with pytest.raises(NsError) as e1:
        update.resync_venvs(tmp_path, paths, uv="uv", run=lambda cmd: (2, "no matching distribution"))
    assert e1.value.code == "NS-E702" and "no matching" in e1.value.detail
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    with pytest.raises(NsError) as e2:
        update.resync_venvs(tmp_path, paths)
    assert e2.value.code == "NS-E702" and "uv" in e2.value.detail


def _inside(child: Path, parent: Path) -> bool:
    child, parent = child.resolve(), parent.resolve()
    return child == parent or parent in child.parents


def test_update_leaves_the_app_directory_before_the_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shortcuts start ns.exe with <app> as cwd; Windows refuses to rename a directory that is a cwd.
    app, z = make_app(tmp_path), make_zip(tmp_path)
    home = tmp_path / "ns"
    home.mkdir()
    monkeypatch.chdir(app)
    seen: list[Path] = []
    e = Env()

    def untracked(a: Path) -> list[str]:
        seen.append(Path.cwd())  # called right before <app> is renamed to <app>.prev
        return ["local_notes.txt"]

    env = e.env()
    env.untracked = untracked
    update.update_app(z, app, env, paths=Paths(home))
    assert seen and not _inside(seen[0], app)
    assert Path.cwd().resolve() == home.resolve()
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"


def test_update_with_a_relative_zip_and_cwd_inside_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, z = make_app(tmp_path), make_zip(tmp_path)
    monkeypatch.chdir(tmp_path)
    update.update_app(Path(z.name), Path("NightShift"), Env().env(), paths=Paths(tmp_path / "ns"))
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
    monkeypatch.chdir(app)
    update.update_app(make_zip(tmp_path, "0.3.0"), app, Env().env(), paths=Paths(tmp_path / "ns"))
    assert not _inside(Path.cwd(), app)  # falls back to the temp dir when the home does not exist
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.3.0"


def test_rollback_also_leaves_the_app_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app(tmp_path)
    paths = Paths(tmp_path / "ns")
    update.update_app(make_zip(tmp_path), app, Env().env(), paths=paths)
    monkeypatch.chdir(app)
    update.rollback_app(app, Env().env(), paths=paths)
    assert not _inside(Path.cwd(), app) and not _inside(Path(os.getcwd()), tmp_path / "NightShift.prev")
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"


def test_update_resync_installs_exactly_the_extras_setup_installed(tmp_path: Path) -> None:
    """s04 (setup) and resync_venvs (update) must agree per venv, tts included."""
    from installer.stage2.simulate import FakeRunner
    from installer.stage2.steps.s04_venvs import S04Venvs
    from installer.stage2.testing import example_answers, make_test_context

    setup_runner = FakeRunner(tmp_path / "desktop")
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, runner=setup_runner)
    ctx.set_flag("tts_fallback", True)
    S04Venvs().run(ctx)

    def installed(tag: str) -> str:
        argv = next(c for c in setup_runner.calls if c.tag == tag).argv
        spec = argv[argv.index("-e") + 1]
        return spec[spec.index("[") :]

    paths = ctx.paths
    for venv in ("studio", "supervisor", "tts"):
        assert paths.venvs.joinpath(venv, "Scripts", "python.exe").exists()
    cmds: list[list[str]] = []
    update.resync_venvs(tmp_path / "app", paths, uv="uv", run=lambda c: (cmds.append(c) or 0, ""))
    by_venv = {Path(c[c.index("--python") + 1]).parts[-3]: c[4] for c in cmds}
    assert by_venv["studio"].endswith(installed("uv pip install studio app"))
    assert by_venv["supervisor"].endswith(installed("uv pip install supervisor app"))
    assert by_venv["tts"].endswith(installed("uv pip install tts app"))


def test_update_and_rollback_write_the_finished_folder_marker(tmp_path: Path) -> None:
    # INSTALL.cmd treats <home>\app without .ns-app.done as an unfinished copy and deletes it.
    from installer.stage2.web import appcopy

    assert update.APP_MARKER == appcopy.MARKER
    app, paths = make_app(tmp_path), Paths(tmp_path / "ns")
    (app / "manifest.json").write_text("{}", encoding="utf-8")
    z = make_zip(tmp_path, extra={"manifest.json": '{"version": "0.2.0"}', "pyproject.toml": "[project]\n"})
    update.update_app(z, app, Env().env(), paths=paths)
    marker = app / appcopy.MARKER
    assert marker.read_text(encoding="utf-8") == appcopy.fingerprint(app)      # the text appcopy writes
    assert appcopy.fingerprint(app).startswith("0.2.0|")
    update.rollback_app(app, Env().env(), paths=paths)
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert marker.read_text(encoding="utf-8") == appcopy.fingerprint(app)
    assert appcopy.fingerprint(app).startswith("0.1.0|")
    update.rollback_app(app, Env().env(), paths=paths)                         # roll forward again
    assert marker.read_text(encoding="utf-8") == appcopy.fingerprint(app)
    assert appcopy.fingerprint(app).startswith("0.2.0|")


def test_a_failed_update_leaves_the_old_folder_as_it_was(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with pytest.raises(NsError):
        update.update_app(make_zip(tmp_path), app, Env(verify_ok=False).env(), paths=Paths(tmp_path / "ns"))
    assert not (app / ".ns-app.done").exists()


def test_a_later_install_swaps_the_updated_home_app_and_stops_the_engine_first(tmp_path: Path) -> None:
    from installer.stage2.web import appcopy
    from tests.test_installer_app_copy import make_source

    home = tmp_path / "home"
    app = appcopy.app_dir(home)
    app.parent.mkdir()
    make_app(tmp_path / "seed").rename(app)
    update.update_app(make_zip(tmp_path), app, Env().env(), paths=Paths(home))
    assert appcopy.installed_version(home) == "0.2.0"          # "Nightshift 0.2.0 is installed here"
    stopped: list[bool] = []
    res = appcopy.install_app(
        make_source(tmp_path / "extracted", "0.3.0"), home, stop_engine=lambda: stopped.append(app.is_dir()),
        sleep=lambda _s: None,
    )
    assert res.action == "updated" and (res.old_version, res.new_version) == ("0.2.0", "0.3.0")
    assert stopped == [True]                                    # stopped while <home>\app still stood
    assert appcopy.installed_version(home) == "0.3.0"
    assert (home / "app.prev" / "VERSION").read_text(encoding="utf-8").strip() == "0.2.0"
