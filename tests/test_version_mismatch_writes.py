r"""Card version-mismatch-writes: a program never changes an install of another version by accident.

Two tmp installs: A (0.1.14: `app\VERSION`, `config\runtime.json`) and B (this version), which stands for
the install the program under test is in (`paths.home_of_place` is replaced: the test venv's checkout is in
no install). `NS_TEST_USER_HOME` stands for the user-level NIGHTSHIFT_HOME.

The rework (the Head's ruling of 2026-11-12): one guard at the few places every writer passes (the two CLI
roots with ONE table, `jobs.open_env`, the database openers, the engine's and the dashboard's start, the
settings writers), not a call per command. The walk tests below fail for a command or a writing dashboard
route that is in no row of `homeguard.COMMANDS` / `DASHBOARD_WRITES`.

WRITTEN, NOT RUN (the owner's build method of 2026-11-11: tests run once after the block is merged).
"""

from __future__ import annotations

import ast
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import ns_common.paths as nspaths
from ns_common import __version__, commandline, config_snapshot, first_start, homeguard
from ns_common.errors import NsError, load_errors

REPO = Path(__file__).resolve().parents[1]
OLD = "0.1.14"


def make_install(root: Path, version: str | None) -> Path:
    (root / "config").mkdir(parents=True)
    (root / "config" / "runtime.json").write_text('{"written_by": "its own program"}', encoding="utf-8")
    (root / "app").mkdir()
    if version is not None:
        (root / "app" / "VERSION").write_text(version + "\n", encoding="utf-8")
    return root


def config_bytes(home: Path) -> dict[str, bytes]:
    files = sorted(f for f in (home / "config").rglob("*") if f.is_file())
    return {str(f.relative_to(home)): f.read_bytes() for f in files}


def tree_of(home: Path) -> dict[str, bytes]:
    """Every file under `home` with its bytes: what "nothing was changed" is checked against."""
    return {str(f.relative_to(home)): f.read_bytes() for f in sorted(home.rglob("*")) if f.is_file()}


@pytest.fixture
def two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(A, B): the window's NIGHTSHIFT_HOME names A (set in the window: the user-level default is unset),
    the program is in B. Nothing is marked as reading yet (a new process). The one line `Paths.from_env`
    says per process counts as said: it names NS-E128, and whether an earlier test of this process already
    said it must not decide what a test here reads in a command's output (the line has its own test)."""
    a = make_install(tmp_path / "NS-old", OLD)
    b = make_install(tmp_path / "NS-new", __version__)
    monkeypatch.setattr(nspaths, "home_of_place", lambda *args, **kw: b)
    monkeypatch.setattr(homeguard, "_reading", set())
    monkeypatch.setattr(nspaths, "_said_place", ["said by an earlier command of this process"])
    monkeypatch.setenv(nspaths.HOME_ENV, str(a))
    monkeypatch.delenv(nspaths.EXPLICIT_ENV, raising=False)
    monkeypatch.delenv(nspaths.TEST_USER_HOME_ENV, raising=False)
    return a, b


SETTINGS_FILES = sorted(config_snapshot.FILES)


def test_the_code_is_in_the_contract() -> None:
    assert homeguard.CODE == "NS-E128"
    assert homeguard.CODE in load_errors()


# ---------------------------------------------------------------------------------------------------------
# the one decision
# ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", SETTINGS_FILES)
def test_a_settings_write_into_an_install_of_another_version_is_refused(
    two: tuple[Path, Path], name: str
) -> None:
    a, b = two
    before = config_bytes(a)
    with pytest.raises(NsError) as caught:
        first_start.guard(a / "config" / name)
    assert caught.value.code == "NS-E128"
    said = str(caught.value)
    assert str(a) in said and str(b) in said
    assert f"version {OLD}" in said and f"Nightshift {__version__}" in said
    assert "Nothing was changed." in said
    assert config_bytes(a) == before
    assert not list(a.parent.glob("*.zip")) and not (a / "logs").exists()    # no copy, no log: nothing at all


def test_a_channel_file_of_the_other_install_is_refused_too(two: tuple[Path, Path]) -> None:
    a, _ = two
    tree = sorted(config_snapshot.TREES)[0]
    with pytest.raises(NsError) as caught:
        first_start.guard(a / "config" / tree / "x.yaml")
    assert caught.value.code == "NS-E128"
    assert not (a / "config" / tree).exists()


def test_a_reading_command_never_changes_settings_either(two: tuple[Path, Path]) -> None:
    a, _ = two
    homeguard.enter("studio status", [])
    with pytest.raises(NsError) as caught:
        first_start.guard(a / "config" / "runtime.json")
    assert caught.value.code == "NS-E128"


def test_the_same_version_is_not_refused(two: tuple[Path, Path]) -> None:
    a, _ = two
    (a / "app" / "VERSION").write_text(__version__ + "\n", encoding="utf-8")
    v = homeguard.decide(a)
    assert (v.foreign, v.why) == (False, homeguard.WHY_SAME_VERSION)
    homeguard.require_own(a, "ns tune")
    homeguard.enter("ns tune", [])
    assert homeguard.may_write(a)
    first_start.guard(a / "config" / "runtime.json")     # goes on to the first-start check, raises nothing


def test_a_vouched_home_is_not_refused(two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    a, _ = two
    monkeypatch.setenv(nspaths.EXPLICIT_ENV, str(a))
    v = homeguard.decide(a)
    assert (v.foreign, v.why) == (False, homeguard.WHY_VOUCHED)
    homeguard.enter("studio run", [])
    first_start.guard(a / "config" / "runtime.json")


def test_a_stale_marker_for_another_folder_does_not_vouch(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = two
    monkeypatch.setenv(nspaths.EXPLICIT_ENV, str(b))
    assert homeguard.decide(a).foreign


def test_the_default_variable_still_gives_way_to_the_programs_install(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = two
    monkeypatch.setenv(nspaths.TEST_USER_HOME_ENV, str(a))      # A is the PC's default, not set in the window
    assert nspaths.Paths.from_env().home == b
    assert (homeguard.decide(b).foreign, homeguard.decide(b).why) == (False, homeguard.WHY_OWN)
    assert (homeguard.decide(a).foreign, homeguard.decide(a).why) == (False, homeguard.WHY_DEFAULT)
    homeguard.enter("ns schedule install", [])           # the command works on B: nothing to refuse


def test_the_programs_own_install_is_never_refused_whatever_its_version_file_says(
    two: tuple[Path, Path]
) -> None:
    """An update, a rollback and a Repair swap the program under its own home: in the moments in between,
    `app\\VERSION` and the running program differ, and the running program must still write."""
    _, b = two
    for text in ("9.9.9\n", "", None):
        if text is None:
            (b / "app" / "VERSION").unlink()
        else:
            (b / "app" / "VERSION").write_text(text, encoding="utf-8")
        assert (homeguard.decide(b).foreign, homeguard.decide(b).why) == (False, homeguard.WHY_OWN)
        assert homeguard.may_write(b)
        first_start.guard(b / "config" / "runtime.json")


def test_a_home_the_code_named_itself_is_not_refused(two: tuple[Path, Path], tmp_path: Path) -> None:
    c = make_install(tmp_path / "NS-third", OLD)
    v = homeguard.decide(c)                 # NIGHTSHIFT_HOME names A, not C: not the window's accident
    assert (v.foreign, v.why) == (False, homeguard.WHY_NOT_BY_VARIABLE)
    assert homeguard.may_write(c)


def test_a_program_in_no_install_is_never_refused(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = two
    monkeypatch.setattr(nspaths, "home_of_place", lambda *args, **kw: None)   # a checkout, the worker's copy
    v = homeguard.decide(a)
    assert (v.foreign, v.why) == (False, homeguard.WHY_NO_PLACE)
    homeguard.enter("ns update", ["--rollback"])
    assert homeguard.may_write(a)
    first_start.guard(a / "config" / "runtime.json")


@pytest.mark.parametrize("how", ["missing", "empty", "not-text"])
def test_an_unreadable_version_counts_as_different_and_names_the_way_forward(
    two: tuple[Path, Path], how: str
) -> None:
    """A killed update or rollback can leave `app\\VERSION` missing: the words name what never depends on
    this program (that install's own shortcuts, INSTALL.cmd's Repair)."""
    a, _ = two
    f = a / "app" / "VERSION"
    if how == "missing":
        f.unlink()
    elif how == "empty":
        f.write_text("  \n", encoding="utf-8")
    else:
        f.write_bytes(b"\xff\xfe\xff")
    with pytest.raises(NsError) as caught:
        homeguard.require_own(a, "ns tune")
    said = str(caught.value)
    assert caught.value.code == "NS-E128"
    assert "its version could not be read" in said and str(f) in said
    assert "its own shortcuts finish it" in said and "run INSTALL.cmd" in said and "choose Repair" in said


def test_a_readable_version_has_no_words_about_an_interrupted_update(two: tuple[Path, Path]) -> None:
    a, _ = two
    assert "INSTALL.cmd" not in homeguard.sentence(homeguard.decide(a), "ns tune")


def test_reading_is_never_refused_and_the_notice_names_both_versions(two: tuple[Path, Path]) -> None:
    a, b = two
    assert homeguard.notice(a) == f"(Nightshift {OLD} there, {__version__} here)"
    assert homeguard.notice(b) == ""
    assert homeguard.decide(a).foreign       # deciding and noticing raise nothing: a reader goes on


def test_the_line_of_a_reading_command_says_the_mismatch_once(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    a, _ = two
    monkeypatch.setattr(nspaths, "_said_place", [])
    assert nspaths.Paths.from_env().home == a
    assert nspaths.Paths.from_env().home == a
    err = capsys.readouterr().err
    assert err.count(f"(Nightshift {OLD} there, {__version__} here)") == 1
    assert err.count("NS-E128") == 1


def test_the_shim_of_the_other_install_is_named(two: tuple[Path, Path]) -> None:
    a, b = two
    v = homeguard.decide(a)
    form = commandline.command_form      # the path as a terminal takes it (quoted when it has a space)
    without = homeguard.sentence(v, "ns tune")
    assert f"command: {form(str(a / 'venvs' / 'supervisor' / 'Scripts' / 'ns.exe'))} tune. " in without
    assert f"run {form(str(b / 'venvs' / 'supervisor' / 'Scripts' / 'ns.exe'))} there." in without
    (a / "cmd").mkdir()
    (a / "cmd" / "ns.cmd").write_text("@echo off\n", encoding="utf-8")
    (a / "cmd" / "studio.cmd").write_text("@echo off\n", encoding="utf-8")
    ns_shim, studio_shim = form(str(a / "cmd" / "ns.cmd")), form(str(a / "cmd" / "studio.cmd"))
    assert f"command: {ns_shim} schedule install. " in homeguard.sentence(v, "ns schedule install")
    assert f"command: {studio_shim} pause. " in homeguard.sentence(v, "studio pause")
    assert f"command: {studio_shim} channel add. " in homeguard.sentence(v, "studio channel add")
    assert f"command: {ns_shim}. " in homeguard.sentence(v)         # a writer's refusal: no command known


def test_a_home_with_a_space_gets_a_command_a_terminal_takes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = make_install(tmp_path / "NS old", OLD)
    b = make_install(tmp_path / "NS-new", __version__)
    monkeypatch.setattr(nspaths, "home_of_place", lambda *args, **kw: b)
    monkeypatch.setenv(nspaths.HOME_ENV, str(a))
    monkeypatch.delenv(nspaths.EXPLICIT_ENV, raising=False)
    monkeypatch.delenv(nspaths.TEST_USER_HOME_ENV, raising=False)
    said = homeguard.sentence(homeguard.decide(a), "ns tune")
    assert commandline.by_full_path(a, "ns tune") in said
    bare = f"{a / 'venvs' / 'supervisor' / 'Scripts' / 'ns.exe'} tune"
    assert bare not in said             # a terminal would split the bare path at the space


def test_nothing_is_remembered_between_two_calls(two: tuple[Path, Path]) -> None:
    """Second run: the other install is updated to this version while this process lives."""
    a, _ = two
    assert homeguard.decide(a).foreign
    (a / "app" / "VERSION").write_text(__version__, encoding="utf-8")
    assert not homeguard.decide(a).foreign
    (a / "app" / "VERSION").write_text(OLD, encoding="utf-8")
    assert homeguard.decide(a).foreign


# ---------------------------------------------------------------------------------------------------------
# the one table: every command and every writing route is in a row
# ---------------------------------------------------------------------------------------------------------
def _walk(command: Any, words: list[str]) -> Iterator[str]:
    subs = getattr(command, "commands", None)
    if subs is None:
        yield " ".join(words)
        return
    if len(words) > 1 and getattr(command, "invoke_without_command", False):
        yield " ".join(words)               # a group that runs by itself (`studio review`, `studio clip`)
    for name, sub in subs.items():
        yield from _walk(sub, [*words, name])


def registered_commands() -> set[str]:
    from typer.main import get_command

    from ns_studio import cli as studio_cli
    from ns_supervisor import cli as ns_cli

    return {*_walk(get_command(ns_cli.app), ["ns"]), *_walk(get_command(studio_cli.app), ["studio"])}


def test_every_registered_command_is_in_a_row_of_the_table() -> None:
    """A new command that is in no row fails here: it cannot be added without saying whether it writes."""
    have = registered_commands()
    missing = sorted(have - set(homeguard.COMMANDS))
    assert not missing, f"{missing}: add each to homeguard.COMMANDS (reads or writes) and to rework-table.md"
    stale = sorted(set(homeguard.COMMANDS) - have)
    assert not stale, f"{stale} are rows of homeguard.COMMANDS but no command"
    assert len(have) == 47


def test_the_table_agrees_with_the_command_list_of_the_mentions() -> None:
    """`commandline.COMMANDS` (FN-3) lists the same commands (tests/test_commands_on_path.py holds it equal to
    the CLIs): one more check that the table names nothing that does not exist."""
    words = set()
    for program, commands in commandline.COMMANDS.items():
        for name, subs in commands.items():
            words |= {f"{program} {name} {s}" for s in subs} if subs else {f"{program} {name}"}
    assert words <= set(homeguard.COMMANDS)


def _writing_routes(rel: str) -> set[str]:
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    out: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in fn.decorator_list:
            if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.args):
                continue
            path = d.args[0].value if isinstance(d.args[0], ast.Constant) else None
            if not isinstance(path, str):
                continue
            verb = d.func.attr.upper()
            if verb in ("POST", "PUT", "DELETE", "PATCH"):
                out.add(f"{verb} {path}")
            elif verb == "API_ROUTE":
                for k in d.keywords:
                    if k.arg == "methods" and isinstance(k.value, (ast.List, ast.Tuple)):
                        out |= {
                            f"{e.value} {path}" for e in k.value.elts
                            if isinstance(e, ast.Constant) and e.value in ("POST", "PUT", "DELETE", "PATCH")
                        }
    return out


def test_every_writing_route_of_the_dashboard_is_in_the_table() -> None:
    have = _writing_routes("ns_dashboard/server.py")
    assert have == set(homeguard.DASHBOARD_WRITES), (
        sorted(have - homeguard.DASHBOARD_WRITES), sorted(homeguard.DASHBOARD_WRITES - have)
    )
    assert len(have) == 48


@pytest.mark.parametrize(
    ("command", "args", "access"),
    [
        ("ns tune", [], homeguard.WRITES),
        ("ns tune", ["--dry-run"], homeguard.READS),
        ("ns update", ["x.zip"], homeguard.WRITES),
        ("ns update", ["--check"], homeguard.READS),
        ("ns doctor", ["--bundle"], homeguard.READS),
        ("studio run", ["--daily"], homeguard.WRITES),
        ("studio run", ["--dry-run", "--daily"], homeguard.READS),
        ("studio run", ["--", "--dry-run"], homeguard.WRITES),         # after `--` it is no option
        ("studio series design", ["s"], homeguard.READS),
        ("studio series design", ["s", "--format=x"], homeguard.WRITES),
        ("studio status", [], homeguard.READS),
        ("studio a-command-nobody-listed", [], homeguard.WRITES),    # fail closed
    ],
)
def test_access_comes_from_the_table_and_an_unlisted_command_writes(
    command: str, args: list[str], access: str
) -> None:
    assert homeguard.access_of(command, args) == access


@pytest.mark.parametrize(
    ("program", "argv", "words", "rest"),
    [
        ("ns", ["schedule", "install", "--time", "03:00"], "ns schedule install", ["--time", "03:00"]),
        ("ns", ["tune", "--dry-run"], "ns tune", ["--dry-run"]),
        ("studio", ["review", "--channel", "approve", "approve", "o1"], "studio review approve", ["o1"]),
        ("studio", ["review", "--channel", "s"], "studio review", ["--channel", "s"]),
        ("studio", ["clip", "v.mp4", "--channel", "s"], "studio clip", ["v.mp4", "--channel", "s"]),
        ("studio", ["series", "design", "s", "--format", "f"], "studio series design",
         ["s", "--format", "f"]),
    ],
)
def test_the_command_words_are_found_through_options_and_their_values(
    program: str, argv: list[str], words: str, rest: list[str]
) -> None:
    from typer.main import get_command

    from ns_studio import cli as studio_cli
    from ns_supervisor import cli as ns_cli
    from ns_supervisor.homecheck import command_path

    root = get_command(ns_cli.app if program == "ns" else studio_cli.app)
    first = root.commands[argv[0]]  # type: ignore[attr-defined]
    assert command_path(program, argv[0], first, argv[1:]) == (words, rest)


@pytest.mark.parametrize(
    ("program", "argv", "options"),
    [
        ("ns", ["tune", "--dry-run"], ["--dry-run"]),
        ("ns", ["tune", "--help"], ["--help"]),
        ("ns", ["schedule", "install", "--time", "--help"], ["--time"]),          # the time is "--help"
        ("ns", ["update", "x.zip", "--log", "--check"], ["--log"]),               # the log file is "--check"
        ("ns", ["update", "--log", "--home", "x.zip"], ["--log"]),                # no home was named
        ("ns", ["update", "x.zip", "--worker", "--home=D:/h", "--wait-pid", "7"],
         ["--worker", "--home=D:/h", "--wait-pid"]),
        ("studio", ["run", "--channel", "--dry-run"], ["--channel"]),             # the channel is "--dry-run"
        ("studio", ["run", "--dry-run", "--channel", "s"], ["--dry-run", "--channel"]),
        ("studio", ["run", "--", "--dry-run"], []),
        ("studio", ["pause", "s", "--reason", "--help"], ["--reason"]),
        ("studio", ["review", "--channel", "s", "show", "o1", "--play"], ["--channel", "--play"]),
        ("studio", ["clip", "v.mp4", "--channel", "s", "--confirm-rights"],
         ["--channel", "--confirm-rights"]),
        ("studio", ["series", "design", "s", "--voice", "--help"], ["--voice"]),
    ],
)
def test_an_option_counts_only_where_click_reads_it_as_one(
    program: str, argv: list[str], options: list[str]
) -> None:
    """Fresh review of 5d9d1db9: `--help`, `--dry-run`, `--check` and `--home` given as the VALUE of another
    option are no option: the command runs (and writes), so the root must not take them for one."""
    from typer.main import get_command

    from ns_studio import cli as studio_cli
    from ns_supervisor import cli as ns_cli
    from ns_supervisor.homecheck import options_given

    root = get_command(ns_cli.app if program == "ns" else studio_cli.app)
    first = root.commands[argv[0]]  # type: ignore[attr-defined]
    assert options_given(first, argv[1:]) == options


# ---------------------------------------------------------------------------------------------------------
# C1 / C2: the two roots, before any command runs
# ---------------------------------------------------------------------------------------------------------
def never(reached: list[str], name: str) -> Callable[..., Any]:
    def call(*args: object, **kw: object) -> Any:
        reached.append(name)
        raise AssertionError(f"{name} was reached after a refusal was due")
    return call


NS_REFUSED = [
    ["stop"], ["verify"], ["verify", "--downloads"], ["verify", "--quick"], ["tune"],
    ["update", "x.zip"], ["update", "--rollback"], ["update", "--github"], ["update", "--git"],
    ["restore", "nightshift-backup-x.zip"], ["restore", "b.zip", "--all"],
    ["schedule", "install"], ["schedule", "install", "--time", "03:00", "--enable"], ["schedule", "remove"],
    ["notify", "t", "m"], ["models", "get", "voicedesign"],
    # an option word that is the VALUE of another option is no option: these run, so they are refused
    ["schedule", "install", "--time", "--help"], ["update", "x.zip", "--log", "--check"],
    ["update", "--log", "--home", "x.zip"],
]


@pytest.mark.parametrize("argv", NS_REFUSED, ids=[" ".join(a) for a in NS_REFUSED])
def test_every_writing_command_of_ns_is_refused_before_it_runs(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    from ns_supervisor import backup, cli, control, kitnights, models_get, notify, update, update_job

    a, b = two
    reached: list[str] = []
    for owner, name in [
        (cli, "_schedule_call"), (cli, "_schedule_setup"), (cli, "_client"), (cli, "_apply_update"),
        (cli, "_update_check"), (control, "read_runtime"), (control, "stop"), (control, "app_path"),
        (kitnights, "owner_chose"), (models_get, "get_model"), (update, "verify_package"),
        (update, "rollback_refusal"), (update_job, "busy"), (update_job, "launch"),
        (notify, "toast_or_note"), (backup, "restore_backup"),
    ]:
        monkeypatch.setattr(owner, name, never(reached, name))
    before_a, before_b = tree_of(a), tree_of(b)
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 1, result.output
    assert reached == []
    assert "NS-E128" in result.output and "Nothing was changed." in result.output
    assert tree_of(a) == before_a and tree_of(b) == before_b
    assert sorted(p.name for p in a.iterdir()) == ["app", "config"]      # no logs, no data, no run folder


STUDIO_REFUSED = [
    ["run"], ["run", "--daily"], ["run", "--resume", "j1"],
    ["channel", "add", "--slug", "s", "--name", "n", "--niche", "x"],
    ["channel", "edit", "s", "--set", "name=1"],
    ["voice", "list"], ["voice", "preview", "v"], ["voice", "design", "v", "--description", "d"],
    ["voice", "clone", "v", "--audio", "a.wav", "--text", "t"], ["voice", "apply-pending"],
    ["review", "approve", "o1"], ["review", "reject", "o1"], ["review", "--channel", "s", "approve", "o1"],
    ["mark-uploaded", "j", "long", "https://youtu.be/x"], ["pause", "s"], ["resume", "s", "--yes"],
    ["dashboard"], ["learn", "--import", "x.csv"], ["calibrate", "--channel", "s"],
    ["calibrate", "--channel", "s", "--score", "x.csv"],
    ["clip", "v.mp4", "--channel", "s", "--confirm-rights"],
    ["series", "start", "s"], ["series", "produce", "slug"], ["series", "design", "slug", "--format", "f"],
    ["ask", "hello"],
    # an option word that is the VALUE of another option is no option: these run, so they are refused
    ["run", "--channel", "--dry-run"], ["pause", "s", "--reason", "--help"],
]


@pytest.mark.parametrize("argv", STUDIO_REFUSED, ids=[" ".join(a[:3]) for a in STUDIO_REFUSED])
def test_every_writing_command_of_studio_is_refused_before_it_runs(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    from ns_studio import cli, dashboard_cmd, jobs
    from ns_studio.clipper import cli as clip_cli
    from ns_studio.series import cli as series_cli

    a, b = two
    reached: list[str] = []
    for owner, name in [
        (cli, "job_gate"), (jobs, "open_env"), (jobs, "load_stage_modules"), (jobs, "db_is_new"),
        (dashboard_cmd, "ensure_dashboard"), (clip_cli, "job_gate"), (series_cli, "_workshop"),
    ]:
        monkeypatch.setattr(owner, name, never(reached, name))
    before_a, before_b = tree_of(a), tree_of(b)
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 1, result.output
    assert reached == []
    assert "NS-E128" in result.output
    assert tree_of(a) == before_a and tree_of(b) == before_b
    assert sorted(p.name for p in a.iterdir()) == ["app", "config"]


READERS = [
    ("ns", ["doctor"]), ("ns", ["doctor", "--bundle"]), ("ns", ["tune", "--dry-run"]), ("ns", ["backup"]),
    ("ns", ["schedule", "show"]), ("ns", ["update", "--check"]),
    ("studio", ["status"]), ("studio", ["channel", "list"]), ("studio", ["channel", "validate"]),
    ("studio", ["review"]), ("studio", ["review", "--channel", "s"]), ("studio", ["review", "show", "o1"]),
    ("studio", ["run", "--dry-run"]), ("studio", ["series", "list"]), ("studio", ["series", "status", "s"]),
    ("studio", ["series", "design", "s"]),
]


@pytest.mark.parametrize(("program", "argv"), READERS, ids=[f"{p} {' '.join(a)}" for p, a in READERS])
def test_reading_commands_pass_the_root_and_are_marked_as_reading(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, program: str, argv: list[str]
) -> None:
    """Each reader gets past its root (the first thing its body does is replaced by a stop) and its process
    is marked as reading A, so `open_env` and the database openers open A without writing."""
    from ns_studio import cli as studio_cli
    from ns_studio import jobs
    from ns_studio.series import cli as series_cli
    from ns_supervisor import backup, control, doctor, update
    from ns_supervisor import cli as ns_cli

    a, _ = two
    reached: list[str] = []

    def stop(name: str) -> Callable[..., Any]:
        def call(*args: object, **kw: object) -> Any:
            reached.append(name)
            raise NsError("NS-E204", "stop here: the reader got past its root")
        return call

    for owner, name in [
        (ns_cli, "_client"), (doctor, "build_bundle"), (control, "read_runtime"), (backup, "make_backup"),
        (ns_cli, "_schedule_call"), (ns_cli, "_update_check"), (control, "app_path"),
        (update, "current_version"), (jobs, "open_env"), (jobs, "prepare_plan"), (series_cli, "_workshop"),
    ]:
        monkeypatch.setattr(owner, name, stop(name))
    monkeypatch.setattr(jobs, "db_is_new", lambda _paths: False)
    monkeypatch.setattr(jobs, "load_stage_modules", lambda *_a, **_k: [])
    monkeypatch.setattr(series_cli, "job_gate", lambda *_a, **_k: None)   # (`series design` passes the gate)
    result = CliRunner().invoke(ns_cli.app if program == "ns" else studio_cli.app, argv)
    assert "NS-E128" not in result.output, result.output
    assert reached, result.output
    assert homeguard._key(a) in homeguard._reading


def test_ns_update_with_home_is_vouched_by_its_own_command_line(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The update worker and the relaunch name the home with `--home`: never refused, whatever the window's
    variable names (the command sets the marker itself, after the root)."""
    from ns_supervisor import cli, control, update

    a, _ = two
    seen: list[Path] = []
    monkeypatch.setattr(control, "app_path", lambda paths: seen.append(paths.home) or paths.home / "app")
    monkeypatch.setattr(update, "current_version", lambda _app: "0.0.0")
    monkeypatch.setattr(cli, "_update_check", lambda _current: None)
    monkeypatch.setenv(nspaths.HOME_ENV, str(a))        # restores both variables the command sets
    monkeypatch.setenv(nspaths.EXPLICIT_ENV, "")
    done = CliRunner().invoke(cli.app, ["update", "--check", "--home", str(a)])
    assert done.exit_code == 0, done.output
    assert seen == [a]


def _calls_in_order(fn: ast.AST) -> list[str]:
    return [
        n.func.attr for n in sorted(
            (n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)),
            key=lambda n: (n.lineno, n.col_offset),
        )
    ]


@pytest.mark.parametrize("rel", ["ns_supervisor/cli.py", "ns_studio/cli.py"])
def test_both_roots_ask_the_table_after_the_install_check_and_no_command_asks_itself(rel: str) -> None:
    """ONE guard per app, at its root: the per-command calls of the first build are gone."""
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    root = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_root")
    order = _calls_in_order(root)
    assert "check_root" in order and "check_home" in order
    assert order.index("check_root") < order.index("check_home")
    asks = sorted(
        n.name for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name != "_root"
        and {"require_own", "enter", "own_home", "_own", "check_home"} & set(_calls_in_order(n))
    )
    assert asks == []


# ---------------------------------------------------------------------------------------------------------
# C3 / C4: open_env and the database openers
# ---------------------------------------------------------------------------------------------------------
def _a_database(a: Path) -> Path:
    db = a / "data" / "studio.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("create table t (x integer)")
    conn.execute("insert into t values (1)")
    conn.commit()
    conn.close()
    (a / "config" / "runtime.json").unlink()   # light: no runtime.json, every service a fake
    return db


def test_open_env_refuses_a_caller_no_command_line_decided_about(two: tuple[Path, Path]) -> None:
    from ns_studio import jobs

    a, _ = two
    _a_database(a)
    before = tree_of(a)
    with pytest.raises(NsError) as caught:
        jobs.open_env(light=True)
    assert caught.value.code == "NS-E128"
    assert tree_of(a) == before
    assert sorted(p.name for p in a.iterdir()) == ["app", "config", "data"]     # no folder made


def test_a_reader_opens_the_other_install_read_only_and_changes_nothing(two: tuple[Path, Path]) -> None:
    from ns_common.db import ReadingDb
    from ns_studio import jobs

    a, _ = two
    db_file = _a_database(a)
    before = db_file.read_bytes()
    homeguard.enter("studio status", [])
    env = jobs.open_env(light=True)
    try:
        assert isinstance(env.db, ReadingDb)
        assert [r["x"] for r in env.db.query("select x from t")] == [1]
        with pytest.raises(NsError) as caught:
            env.db.execute("insert into t values (2)")
        assert caught.value.code == "NS-E128" and "could not be read without changing it" in str(caught.value)
    finally:
        env.db.close()
    assert db_file.read_bytes() == before
    assert sorted(p.name for p in (a / "data").iterdir()) == ["studio.db"]       # no backup, no -wal left
    assert sorted(p.name for p in a.iterdir()) == ["app", "config", "data"]      # not even a logs folder


def test_a_reader_adds_no_line_to_the_other_installs_logs(two: tuple[Path, Path]) -> None:
    """Fresh review of 5d9d1db9, point (a): a reading command's logger made `logs` in the other install and
    added its lines there. A reader writes nothing; a program in its own install logs as always."""
    from ns_common.logging import NsLogger

    a, b = two
    (a / "logs").mkdir()
    (a / "logs" / "studio-20261111.jsonl").write_text('{"event": "its own"}\n', encoding="utf-8")
    before = tree_of(a)
    homeguard.enter("studio status", [])
    assert homeguard.reading(a) and not homeguard.reading(b)
    log = NsLogger("studio", a / "logs")
    log.info("looked", "a reader's line")
    log.warn("looked_again")
    with log.timed("a_look"):
        pass
    assert tree_of(a) == before
    (a / "logs" / "studio-20261111.jsonl").unlink()
    (a / "logs").rmdir()
    NsLogger("studio", a / "logs").error("no_folder_either")
    assert not (a / "logs").exists()
    own = NsLogger("studio", b / "logs")
    own.info("wrote")
    assert len(list((b / "logs").glob("studio-*.jsonl"))) == 1


def test_a_process_that_marked_nothing_logs_as_always(two: tuple[Path, Path]) -> None:
    """Only `enter` mutes a logger (a reading command of this process): the guard at each choke point, not
    the logger, is what refuses a writer."""
    from ns_common.logging import NsLogger

    a, _ = two
    assert not homeguard.reading(a)
    NsLogger("supervisor", a / "logs").info("a line")
    assert len(list((a / "logs").glob("supervisor-*.jsonl"))) == 1


def test_a_reader_of_a_missing_database_is_told_and_no_file_is_made(two: tuple[Path, Path]) -> None:
    from ns_studio import jobs

    a, _ = two
    _a_database(a).unlink()
    homeguard.enter("studio status", [])
    with pytest.raises(NsError) as caught:
        jobs.open_env(light=True)
    said = str(caught.value)
    assert caught.value.code == "NS-E128"
    assert "could not be read without changing it" in said and "To see" in said
    assert not (a / "data" / "studio.db").exists()


def test_the_database_openers_ask_first(two: tuple[Path, Path]) -> None:
    from ns_common.db import ReadingDb, open_assistant_db, open_studio_db

    a, _ = two
    paths = nspaths.Paths(a)
    for opener in (open_studio_db, open_assistant_db):
        with pytest.raises(NsError):
            opener(paths, None, note=lambda _m: None)
    assert not (a / "data").exists()
    homeguard.enter("ns backup", [])
    assert isinstance(open_studio_db(paths, None, note=lambda _m: None), ReadingDb)
    assert not (a / "data").exists()


# ---------------------------------------------------------------------------------------------------------
# C5 / C6: the engine's and the dashboard's start
# ---------------------------------------------------------------------------------------------------------
def test_the_engine_refuses_at_its_start_before_anything_is_written(
    two: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    from ns_supervisor import app as engine

    a, _ = two
    before = tree_of(a)
    assert engine.main([]) == 1
    err = capsys.readouterr().err
    # the way out is that install's own `studio dashboard`, by full path (`sentence`): never the plain words
    assert "NS-E128" in err and f"command: {commandline.by_full_path(a, 'studio dashboard')}. " in err
    assert tree_of(a) == before and sorted(p.name for p in a.iterdir()) == ["app", "config"]


def test_the_dashboard_refuses_at_its_start_before_anything_is_written(
    two: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Blocker 3: never half-way through a settings save; the server stops before `configure_tmp`."""
    from ns_dashboard import handlers, server

    a, _ = two
    before = tree_of(a)
    assert server.main([]) == 1
    assert "NS-E128" in capsys.readouterr().err
    with pytest.raises(NsError) as caught:
        handlers.build_state(nspaths.Paths(a))
    assert caught.value.code == "NS-E128"
    assert tree_of(a) == before and sorted(p.name for p in a.iterdir()) == ["app", "config"]


def test_the_voice_server_refuses_at_its_start_before_anything_is_written(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fresh review of 5d9d1db9: `python -m ns_tts.server` takes its home from the variable too, and wrote
    that install's log and voices. The engine starts it on its own home; a hand start in the accident's
    window is refused before the log."""
    import sys

    import uvicorn

    from ns_tts import server

    a, _ = two
    before = tree_of(a)
    reached: list[str] = []
    monkeypatch.setattr(sys, "argv", ["ns_tts.server", "--engine", "fake"])
    monkeypatch.setattr(uvicorn, "run", never(reached, "uvicorn.run"))    # one that got there never serves
    with pytest.raises(SystemExit) as stopped:
        server.main()
    assert stopped.value.code == 1 and reached == []
    assert "NS-E128" in capsys.readouterr().err
    assert tree_of(a) == before and sorted(p.name for p in a.iterdir()) == ["app", "config"]


def test_the_dashboards_job_child_refuses_before_its_handler(
    two: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`python -m ns_dashboard.studio_job download` writes that install's imports folder through no database
    and no `open_env`: the child asks at its start, as an `error` event the dashboard would show."""
    from ns_dashboard import studio_job

    a, _ = two
    reached: list[str] = []
    monkeypatch.setitem(studio_job.HANDLERS, "download", never(reached, "download"))
    args = tmp_path / "args.json"
    args.write_text('{"url": "https://example.invalid/v", "source_id": "s1"}', encoding="utf-8")
    before = tree_of(a)
    assert studio_job.main(["download", "--args", str(args)]) == 1
    out = capsys.readouterr().out
    assert reached == []
    assert studio_job.PREFIX in out and '"ev": "error"' in out and '"code": "NS-E128"' in out
    assert tree_of(a) == before and sorted(p.name for p in a.iterdir()) == ["app", "config"]


def test_a_clip_job_asks_before_it_makes_a_folder(two: tuple[Path, Path]) -> None:
    """`create_clip_job` made the home's folders before its database opener refused: it asks first now, so
    `python -m ns_studio.clipper.cli` (no root of its own) leaves nothing behind."""
    from ns_studio.clipper import stages_clip

    a, _ = two
    before = tree_of(a)
    with pytest.raises(NsError) as caught:      # refused before the channel or the parameters are looked at
        stages_clip.create_clip_job(nspaths.Paths(a), None, None)  # type: ignore[arg-type]
    assert caught.value.code == "NS-E128"
    assert tree_of(a) == before and sorted(p.name for p in a.iterdir()) == ["app", "config"]


# ---------------------------------------------------------------------------------------------------------
# C7: the settings writers
# ---------------------------------------------------------------------------------------------------------
def _calls(fn: ast.AST) -> list[tuple[str, str]]:
    out = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name):
            out.append((n.func.value.id, n.func.attr))
        elif isinstance(n.func, ast.Name):
            out.append(("", n.func.id))
    return out


def test_the_settings_guard_asks_the_version_guard_first() -> None:
    """Every settings writer calls `first_start.guard` (tests/test_first_start_snapshot.py finds them from
    the code); `guard` asks `homeguard` before the first-start check: each of them passes the one function."""
    tree = ast.parse((REPO / "ns_common" / "first_start.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "guard")
    calls = _calls(guard)
    assert ("homeguard", "refuse_write") in calls and ("", "ensure") in calls
    lines = {     # by line: ast.walk is breadth-first
        (n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id): n.lineno  # type: ignore[union-attr]
        for n in ast.walk(guard) if isinstance(n, ast.Call) and isinstance(n.func, (ast.Attribute, ast.Name))
    }
    assert lines["refuse_write"] < lines["ensure"]


def test_a_reader_of_a_torn_install_state_keeps_the_file_and_reads_the_last_good_copy(
    two: tuple[Path, Path]
) -> None:
    """Review d1522e6: in the window accident a READ of a torn install_state.json is not refused and changes
    nothing in that install; the reader gets the `.bak`."""
    from ns_common.atomic import BACKUP_SUFFIX
    from ns_common.config import load_install_state

    a, _ = two
    torn = a / "config" / "install_state.json"
    torn.write_text('{"app_path": "C:/x", "inst', encoding="utf-8")
    good = torn.with_name(torn.name + BACKUP_SUFFIX)
    good.write_text('{"app_path": "C:/the-last-good-copy"}', encoding="utf-8")
    before = tree_of(a)
    state = load_install_state(nspaths.Paths(a))
    assert state.app_path == "C:/the-last-good-copy"    # the `.bak`, not an empty state
    assert tree_of(a) == before                 # the torn file stays for that install's own program
