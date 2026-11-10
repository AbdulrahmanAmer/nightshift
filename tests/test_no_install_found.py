r"""D-35: `ns` and `studio` with no install found refuse with NS-E123 and write nothing.

Every test enters through `typer.testing.CliRunner` on the real `ns_supervisor.cli.app` or
`ns_studio.cli.app`, with NIGHTSHIFT_HOME removed from the test's own environment and LOCALAPPDATA pointing at
`tmp_path/la` (never the real AppData). Nothing here reaches the real Task Scheduler, registry, keyring or
Desktop, and no download or program can start: `subprocess.Popen` and the downloader's `run_plan` are replaced
for every test, and `Paths.from_env` raises where it would resolve to an empty, unnamed home (see `la`), so
the old behaviour fails at an assertion instead of acting.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from ns_common import __version__, first_start
from ns_common.config import save_runtime
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_studio import cli as studio_cli
from ns_supervisor import cli as ns_cli
from ns_supervisor import models_get

runner = CliRunner()


def command_paths(app: typer.Typer) -> list[tuple[str, ...]]:
    """Every registered command of `app` (a leaf, or a group that runs on its own), as the words to type."""
    found: list[tuple[str, ...]] = []

    def walk(group: Any, prefix: tuple[str, ...]) -> None:
        for name, cmd in sorted(group.commands.items()):
            here = (*prefix, name)
            if hasattr(cmd, "commands"):
                if cmd.invoke_without_command:
                    found.append(here)
                walk(cmd, here)
            else:
                found.append(here)

    walk(typer.main.get_command(app), ())  # type: ignore[arg-type]
    return found


NS_COMMANDS = [("ns", *p) for p in command_paths(ns_cli.app)]
STUDIO_COMMANDS = [("studio", *p) for p in command_paths(studio_cli.app)]


def run(tool: str, *args: str, env: dict[str, str] | None = None) -> Any:
    app = ns_cli.app if tool == "ns" else studio_cli.app
    return runner.invoke(app, list(args), env=env)


@pytest.fixture
def reached() -> list[str]:
    """What ran against a home nobody named and nothing installed: must stay empty."""
    return []


@pytest.fixture
def la(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reached: list[str]) -> Path:
    """An empty `%LOCALAPPDATA%` and no NIGHTSHIFT_HOME: the PC where no install is found.

    Safety net for the old behaviour: `Paths.from_env` raises when it would resolve to that empty default
    folder, so a command that is (wrongly) allowed to run there fails at once instead of acting (stopping
    processes, planning downloads, creating jobs). Where the home is named, or the default folder holds an
    install, it is the real `Paths.from_env`.
    """
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)
    folder = tmp_path / "la"
    folder.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(folder))
    # a checkout, not an install: the program's own folder has no install beside it
    monkeypatch.setattr(first_start, "program_dir", lambda: tmp_path / "checkout" / "app")
    real = Paths.from_env.__func__  # type: ignore[attr-defined]

    def from_env(cls: type[Paths], env: Any = None) -> Paths:
        e = os.environ if env is None else env
        paths = real(cls, env)
        named = bool(e.get("NIGHTSHIFT_HOME"))
        installed = any((paths.home / "config" / m).is_file() for m in ("runtime.json", "install_state.json"))
        if not named and not installed:
            reached.append("Paths.from_env")
            raise AssertionError("a command read a home nobody named, where nothing is installed")
        return paths

    monkeypatch.setattr(Paths, "from_env", classmethod(from_env))
    return folder


def nothing_created(folder: Path) -> bool:
    return sorted(folder.rglob("*")) == []


class Plans:
    """Replaces the downloader's `run_plan`: records the call and downloads nothing."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[Any] = []
        from installer.stage2 import downloader

        def record(items: list[Any], **kw: Any) -> Any:
            self.calls.append(items)
            return downloader.PlanResult(downloaded=[i.name for i in items])

        monkeypatch.setattr(downloader, "run_plan", record)


@pytest.fixture(autouse=True)
def plans(monkeypatch: pytest.MonkeyPatch) -> Plans:
    """On every test: no model download can start (the old behaviour began a 2 GB one), and no program can be
    started (schtasks, a toast, an engine): the code that would is replaced by something that only records."""
    def blocked_popen(*a: Any, **kw: Any) -> Any:
        raise AssertionError("a test started a program")

    monkeypatch.setattr(subprocess, "Popen", blocked_popen)
    return Plans(monkeypatch)


@pytest.fixture
def fake_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ns schedule show` without Task Scheduler."""
    monkeypatch.setattr(ns_cli.schedule, "show", lambda paths: "task: none (fake)")


# ------------------------------------------------------------------------------------------ the refusal
def test_ns_models_get_without_a_home_refuses_with_ns_e123_and_writes_nothing(la: Path, plans: Plans) -> None:
    r = run("ns", "models", "get", "voicedesign")
    assert r.exit_code == 1, r.output
    assert "NS-E123" in r.output
    assert str(la / "Nightshift") in r.output           # the folder it looked in
    assert "NIGHTSHIFT_HOME" in r.output                # and the way out
    assert plans.calls == []                            # never planned, never downloaded
    assert nothing_created(la)                          # and it did not make a home by assumption
    assert "belongs to" not in r.output                 # no install of its own: no claim about one


@pytest.mark.parametrize("words", NS_COMMANDS + STUDIO_COMMANDS, ids=lambda w: " ".join(w))
def test_every_ns_and_studio_subcommand_refuses_the_same_way(
    words: tuple[str, ...], la: Path, reached: list[str]
) -> None:
    tool, *rest = words
    r = run(tool, *rest)
    assert r.exit_code == 1, r.output
    assert "NS-E123" in r.output, r.output
    assert str(la / "Nightshift") in r.output
    assert reached == []                                # refused before any Paths use
    assert nothing_created(la)


def test_the_sweep_covers_the_commands_of_both_apps() -> None:
    names = {" ".join(w) for w in NS_COMMANDS + STUDIO_COMMANDS}
    for expected in ("ns stop", "ns models get", "ns schedule install", "ns update", "ns notify",
                     "studio run", "studio channel list", "studio voice list", "studio clip", "studio review",
                     "studio dashboard", "studio series"):
        assert any(n == expected or n.startswith(expected + " ") for n in names), expected
    assert len(NS_COMMANDS) >= 10 and len(STUDIO_COMMANDS) >= 15


def test_the_message_names_the_install_this_program_belongs_to(
    la: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "runtime.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(first_start, "program_dir", lambda: home / "app")
    for tool, words in (("ns", ("models", "get", "voicedesign")), ("studio", ("status",))):
        r = run(tool, *words)
        assert r.exit_code == 1 and "NS-E123" in r.output
        assert f"$env:NIGHTSHIFT_HOME = '{home}'" in r.output, r.output
        assert "Nightshift shortcuts" in r.output
    assert nothing_created(la)


def test_a_quote_in_the_installs_folder_is_doubled_for_powershell(
    la: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "Ann's PC"
    (home / "config").mkdir(parents=True)
    (home / "config" / "install_state.json").write_text("{}", encoding="utf-8")   # the other marker
    monkeypatch.setattr(first_start, "program_dir", lambda: home / "app")
    r = run("ns", "stop")
    assert f"$env:NIGHTSHIFT_HOME = '{str(home).replace(chr(39), chr(39) * 2)}'" in r.output


def test_the_hint_is_the_way_out_and_is_true_of_the_product(la: Path) -> None:
    r = run("ns", "stop")
    hint = r.output.split("Hint:", 1)[1]
    assert "NIGHTSHIFT_HOME" in hint and "%LOCALAPPDATA%" in hint
    assert "new PowerShell window" in hint and "Nothing was downloaded or changed." in hint
    s = run("studio", "status")
    assert "hint:" in s.output and "NIGHTSHIFT_HOME" in s.output


def test_an_empty_nightshift_home_counts_as_unset(la: Path) -> None:
    r = run("ns", "stop", env={"NIGHTSHIFT_HOME": ""})
    assert r.exit_code == 1 and "NS-E123" in r.output
    assert nothing_created(la)


# ------------------------------------------------------------------------------------------ the controls
@pytest.mark.parametrize("marker", ["runtime.json", "install_state.json"])
def test_the_default_folder_with_an_install_is_used_as_today(
    la: Path, fake_schedule: None, marker: str
) -> None:
    config = la / "Nightshift" / "config"
    config.mkdir(parents=True)
    if marker == "runtime.json":
        save_runtime(Paths(la / "Nightshift"), Runtime.default("gpu"))
    else:
        (config / marker).write_text("{}", encoding="utf-8")
    r = run("ns", "schedule", "show")
    assert r.exit_code == 0, r.output
    assert "NS-E123" not in r.output and "task: none (fake)" in r.output
    # a second run, after the first one: still the same
    again = run("ns", "schedule", "show")
    assert again.exit_code == 0 and "NS-E123" not in again.output


def test_a_set_nightshift_home_is_never_refused(la: Path, tmp_path: Path, fake_schedule: None) -> None:
    empty = tmp_path / "empty-home"
    empty.mkdir()
    r = run("ns", "schedule", "show", env={"NIGHTSHIFT_HOME": str(empty)})
    assert r.exit_code == 0, r.output
    assert "NS-E123" not in r.output and "task: none (fake)" in r.output
    missing = tmp_path / "not-there-yet"          # setup runs `ns` before the home is complete
    r2 = run("ns", "schedule", "show", env={"NIGHTSHIFT_HOME": str(missing)})
    assert r2.exit_code == 0 and "NS-E123" not in r2.output
    s = run("studio", "status", env={"NIGHTSHIFT_HOME": str(empty)})
    assert "NS-E123" not in s.output


def test_ns_update_that_names_its_home_with_the_option_is_not_refused(la: Path, tmp_path: Path) -> None:
    # the update worker is started as `ns update ... --worker --home <home>` without trusting the environment
    # it inherits; the check must not refuse it before the option is read. No mode given: `update` stops
    # with its own usage error (exit 2) and touches nothing, which is what proves the check let it through.
    home = tmp_path / "named"
    r = run("ns", "update", "--home", str(home))
    assert r.exit_code == 2 and "NS-E123" not in r.output and "give exactly one of" in r.output
    r2 = run("ns", "update", f"--home={home}")
    assert r2.exit_code == 2 and "NS-E123" not in r2.output
    # an empty value names nothing, and the option on another command names nothing
    assert "NS-E123" in run("ns", "update", "--home", "").output
    assert "NS-E123" in run("ns", "stop", "--home", str(home)).output
    assert os.environ.get("NIGHTSHIFT_HOME") is None
    assert nothing_created(la)


@pytest.mark.parametrize(
    "words",
    [("ns",), ("ns", "--help"), ("studio", "--help"), ("studio",),
     ("ns", "models", "get", "--help"), ("ns", "schedule", "--help"), ("ns", "update", "--help"),
     ("studio", "channel", "--help"), ("studio", "voice", "list", "--help"), ("studio", "run", "--help"),
     ("studio", "review", "--help"), ("studio", "clip", "--help"), ("studio", "series", "--help")],
    ids=lambda w: " ".join(w),
)
def test_bare_ns_and_help_still_answer(words: tuple[str, ...], la: Path) -> None:
    tool, *rest = words
    r = run(tool, *rest)
    assert r.exit_code == 0, r.output
    assert "NS-E123" not in r.output
    assert nothing_created(la)
    if not rest:
        assert f"{tool} {__version__}" in r.output


GROUPS = [("ns", "schedule"), ("ns", "models"), ("studio", "channel"), ("studio", "voice")]


@pytest.mark.parametrize("words", GROUPS)
def test_a_group_named_alone_prints_help_or_usage_not_the_refusal(words: tuple[str, ...], la: Path) -> None:
    tool, *rest = words
    r = run(tool, *rest)
    assert "NS-E123" not in r.output
    assert nothing_created(la)


def test_after_a_literal_double_dash_help_is_a_value_not_a_help_request(la: Path) -> None:
    r = run("ns", "notify", "--", "--help", "x")
    assert r.exit_code == 1 and "NS-E123" in r.output


# ------------------------------------------------------------------------------------------ cannot tell
def test_a_folder_that_cannot_be_looked_at_is_not_called_empty(
    la: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Access denied on config: not proof of no install. The check says nothing and deletes nothing."""
    homecheck = pytest.importorskip("ns_supervisor.homecheck")   # a new module: nothing to import on the base
    real = os.stat

    def denied(path: Any, *a: Any, **kw: Any) -> Any:
        if str(path).endswith(("runtime.json", "install_state.json")):
            raise PermissionError(13, "Access is denied")
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "stat", denied)
    assert homecheck.holds_install(la / "Nightshift") is None
    homecheck.require_install()                          # returns: the command goes on as it always did


def test_the_check_reads_the_environment_it_is_given(tmp_path: Path) -> None:
    homecheck = pytest.importorskip("ns_supervisor.homecheck")
    with pytest.raises(Exception) as caught:
        homecheck.require_install({"LOCALAPPDATA": str(tmp_path / "x")})
    assert getattr(caught.value, "code", "") == "NS-E123"
    homecheck.require_install({"NIGHTSHIFT_HOME": str(tmp_path / "anywhere")})


def test_both_root_groups_keep_the_words_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ns_studio` cannot subclass `InstallGuardGroup` (it reaches `ns_supervisor` lazily); both groups must
    leave the subcommand's words where `check_root` reads them."""
    homecheck = pytest.importorskip("ns_supervisor.homecheck")
    seen: list[Any] = []
    real = homecheck.remember

    def spy(ctx: Any, name: Any, command: Any, rest: Any) -> None:
        seen.append((name, list(rest)))
        real(ctx, name, command, rest)

    monkeypatch.setattr(homecheck, "remember", spy)
    runner.invoke(studio_cli.app, ["channel", "--help"], env={"NIGHTSHIFT_HOME": "x"})
    runner.invoke(ns_cli.app, ["schedule", "--help"], env={"NIGHTSHIFT_HOME": "x"})
    assert seen == [("channel", ["--help"]), ("schedule", ["--help"])]


# ------------------------------------------------------------------------------------------ models get
def test_a_missing_runtime_json_in_a_named_home_stops_models_get_without_a_download(
    la: Path, tmp_path: Path, plans: Plans
) -> None:
    home = tmp_path / "named-empty"
    home.mkdir()
    r = run("ns", "models", "get", "voicedesign", env={"NIGHTSHIFT_HOME": str(home)})
    assert r.exit_code == 1, r.output
    assert "runtime.json" in r.output and "NS-E125" in r.output
    assert "NS-E203" not in r.output and "health check" not in r.output    # that code's text is not true here
    assert plans.calls == []                             # base: assumed the GPU profile and planned its items
    assert not (home / "models").exists()


def test_an_unreadable_runtime_json_stops_models_get_with_its_own_error(
    la: Path, tmp_path: Path, plans: Plans
) -> None:
    home = tmp_path / "named"
    (home / "config").mkdir(parents=True)
    bad = home / "config" / "runtime.json"
    bad.write_text("{", encoding="utf-8")
    r = run("ns", "models", "get", "voicedesign", env={"NIGHTSHIFT_HOME": str(home)})
    assert r.exit_code == 1, r.output
    assert "NS-E125" in r.output and str(bad) in r.output and "could not be read" in r.output
    assert "NS-E203" not in r.output and "health check" not in r.output
    assert "Traceback" not in r.output and "not valid JSON" in r.output   # the reason, in setup's words
    assert plans.calls == []                             # nothing planned
    assert bad.read_text(encoding="utf-8") == "{"       # the file is still there, untouched: never dropped
    assert sorted(p.name for p in bad.parent.iterdir()) == ["runtime.json"]
    again = run("ns", "models", "get", "voicedesign", env={"NIGHTSHIFT_HOME": str(home)})
    assert again.exit_code == 1 and plans.calls == []   # the second run says the same


def test_the_way_out_the_hint_names_works(tmp_path: Path, plans: Plans) -> None:
    """NS-E125's hint: run setup again. Setup writes a missing runtime.json from the answers, and keeps a
    damaged one as runtime.json.bad before it does (the owner renames nothing); after that `ns models get`
    works. (`ns tune`, the obvious guess, reads the file first and cannot repair a missing or damaged one.)"""
    from installer.stage2.steps.s06_tune import S06Tune
    from installer.stage2.testing import example_answers, make_test_context
    from installer.stage2.wizard import EXIT_OK, Wizard

    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    home = ctx.paths.home
    env = {"NIGHTSHIFT_HOME": str(home)}
    for damage in ("missing", "torn"):
        ctx.paths.runtime_file.unlink(missing_ok=True)
        for old in ctx.paths.config.glob("runtime.json.bad*"):
            old.unlink()
        if damage == "torn":
            ctx.paths.runtime_file.parent.mkdir(parents=True, exist_ok=True)
            ctx.paths.runtime_file.write_text("{", encoding="utf-8")
        plans.calls.clear()
        r = run("ns", "models", "get", "voicedesign", env=env)
        assert r.exit_code == 1 and "NS-E125" in r.output and plans.calls == [], (damage, r.output)
        assert "INSTALL.cmd" in r.output and "runtime.json.bad" in r.output
        assert "rename it" not in r.output                                  # the owner renames nothing
        assert Wizard(ctx, [S06Tune()], backoff=()).run() == EXIT_OK        # setup, again
        if damage == "torn":
            assert (ctx.paths.config / "runtime.json.bad").read_text(encoding="utf-8") == "{"
        r2 = run("ns", "models", "get", "voicedesign", env=env)
        assert r2.exit_code == 0 and len(plans.calls) == 1, (damage, r2.output)


def test_get_model_no_longer_assumes_the_gpu_profile(tmp_path: Path, plans: Plans) -> None:
    from ns_common.errors import NsError

    paths = Paths(tmp_path)
    seen: list[Any] = []
    with pytest.raises(NsError):
        models_get.get_model("voicedesign", paths, Path(__file__).resolve().parents[1], seen.append)
    assert seen == [] and plans.calls == []


def test_a_good_runtime_json_still_downloads_as_today(la: Path, tmp_path: Path, plans: Plans) -> None:
    home = tmp_path / "named-good"
    save_runtime(Paths(home), Runtime.default("gpu"))
    r = run("ns", "models", "get", "voicedesign", env={"NIGHTSHIFT_HOME": str(home)})
    assert r.exit_code == 0, r.output
    assert len(plans.calls) == 1 and plans.calls[0]


def test_the_default_folder_is_the_one_paths_falls_back_to(tmp_path: Path) -> None:
    homecheck = pytest.importorskip("ns_supervisor.homecheck")
    env = {"LOCALAPPDATA": str(tmp_path / "a")}
    want = homecheck.default_home(env)
    (want / "config").mkdir(parents=True)
    (want / "config" / "runtime.json").write_text("{}", encoding="utf-8")   # lets the guard pass
    assert Paths.from_env(env).home == want
