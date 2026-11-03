"""C2: `ns update --git [--channel stable|main]` for a cloned-repo install (gap 19)."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import cli, update, update_job
from tests.test_ns_update import Env, make_app

runner = CliRunner()


class FakeGit:
    """Answers the git commands `git_update` uses and records them."""

    def __init__(self, *, dirty: str = "", fail: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.dirty = dirty
        self.fail = fail or {}

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        key = args[0]
        if key in self.fail:
            return self.fail[key]
        if key == "rev-parse":
            return 0, "a" * 40
        if key == "status":
            return 0, self.dirty
        return 0, ""


def run(tmp_path: Path, git: FakeGit, env: Env | None = None, channel: str = "stable"):
    app = make_app(tmp_path)
    paths = Paths(tmp_path / "ns")
    e = env or Env()
    res = update.git_update(app, e.env(), paths=paths, channel=channel, latest_tag=lambda: "v0.2.0", git=git)
    return res, e


def test_stable_moves_to_the_newest_release_tag_and_resyncs(tmp_path: Path) -> None:
    git = FakeGit()
    res, e = run(tmp_path, git)
    assert ["fetch", "--tags", "--prune", "origin"] in git.calls
    assert ["checkout", "--detach", "v0.2.0"] in git.calls
    assert e.calls == ["stop", "sync", "verify", "reregister"] and "channel stable" in res.notes


def test_main_follows_origin_main(tmp_path: Path) -> None:
    git = FakeGit()
    run(tmp_path, git, channel="main")
    assert ["checkout", "--detach", "origin/main"] in git.calls


def test_a_failed_verify_goes_back_to_the_previous_commit(tmp_path: Path) -> None:
    git = FakeGit()
    with pytest.raises(NsError) as ei:
        run(tmp_path, git, Env(verify_ok=False))
    assert ei.value.code == "NS-E702"
    assert git.calls[-1] == ["checkout", "--detach", "a" * 40]


def test_local_changes_stop_everything_before_the_engine_is_touched(tmp_path: Path) -> None:
    git = FakeGit(dirty=" M ns_common/x.py")
    e = Env()
    with pytest.raises(NsError, match="local changes"):
        run(tmp_path, git, e)
    assert e.calls == [] and not any(c[0] == "checkout" for c in git.calls)


def test_a_failed_fetch_is_an_offline_error_and_changes_nothing(tmp_path: Path) -> None:
    git = FakeGit(fail={"fetch": (128, "could not resolve host")})
    e = Env()
    with pytest.raises(NsError) as ei:
        run(tmp_path, git, e)
    assert ei.value.code == "NS-E105" and e.calls == []


def test_a_folder_without_git_is_refused_and_the_channel_is_checked(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    (app / ".git" / "HEAD").unlink()
    (app / ".git").rmdir()
    with pytest.raises(NsError, match="not a git clone"):
        update.git_update(app, Env().env(), paths=Paths(tmp_path / "ns"), git=FakeGit())
    with pytest.raises(NsError, match="unknown channel"):
        update.git_update(app, Env().env(), paths=Paths(tmp_path / "ns"), channel="beta", git=FakeGit())


def test_the_cli_takes_git_and_channel_and_runs_it_in_the_detached_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    started: list[list[str]] = []
    monkeypatch.setattr(update_job, "spawn_detached", lambda argv, cwd: started.append(argv) or 5)
    r = runner.invoke(cli.app, ["update", "--git", "--channel", "main", "--detach"],
                      env={"NIGHTSHIFT_HOME": str(home)})
    assert r.exit_code == 0, r.output
    argv = started[0]
    assert "--git" in argv and argv[argv.index("--channel") + 1] == "main" and "--worker" in argv
    assert json.loads(r.output.strip().splitlines()[-1])["started"] is True


def test_the_cli_rejects_a_bad_channel_or_two_actions(tmp_path: Path) -> None:
    env = {"NIGHTSHIFT_HOME": str(tmp_path)}
    assert runner.invoke(cli.app, ["update", "--git", "--channel", "beta"], env=env).exit_code == 2
    assert runner.invoke(cli.app, ["update", "--github", "--channel", "main"], env=env).exit_code == 2
    assert runner.invoke(cli.app, ["update", "--git", "--github"], env=env).exit_code == 2


def test_without_git_installed_the_message_says_to_install_git_for_this(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path)
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    e = Env()
    with pytest.raises(NsError) as ei:
        update.git_update(app, e.env(), paths=Paths(tmp_path / "ns"), latest_tag=lambda: "v0.2.0")
    assert ei.value.code == "NS-E702" and "Install Git for this" in ei.value.detail
    assert "--github" in ei.value.detail                # and names the way that needs no Git
    assert e.calls == []                                # nothing was stopped or touched


def test_setup_never_installs_git() -> None:
    from installer.stage2.web import bootstrap, plan

    names = [(key + " " + winget).lower() for key, winget, *_ in bootstrap.TOOLS]
    assert not any("git" in name for name in names)
    assert "git" not in {row["key"] for row in plan.tool_rows()}
