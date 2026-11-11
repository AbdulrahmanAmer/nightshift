r"""Card version-mismatch-writes: a program never changes an install of another version by accident.

Two tmp installs: A (0.1.14: `app\VERSION`, `config\runtime.json`) and B (this version), which stands for
the install the program under test is in (`paths.home_of_place` is replaced: the test venv's checkout is in
no install). `NS_TEST_USER_HOME` stands for the user-level NIGHTSHIFT_HOME.

WRITTEN, NOT RUN (the owner's build method of 2026-11-11: tests run once after the block is merged).
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ns_common.paths as nspaths
from ns_common import __version__, config_snapshot, first_start, homeguard
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


@pytest.fixture
def two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(A, B): the window's NIGHTSHIFT_HOME names A (set in the window: the user-level default is unset),
    the program is in B."""
    a = make_install(tmp_path / "NS-old", OLD)
    b = make_install(tmp_path / "NS-new", __version__)
    monkeypatch.setattr(nspaths, "home_of_place", lambda *args, **kw: b)
    monkeypatch.setenv(nspaths.HOME_ENV, str(a))
    monkeypatch.delenv(nspaths.EXPLICIT_ENV, raising=False)
    monkeypatch.delenv(nspaths.TEST_USER_HOME_ENV, raising=False)
    return a, b


SETTINGS_FILES = sorted(config_snapshot.FILES)


def test_the_code_is_in_the_contract() -> None:
    assert homeguard.CODE == "NS-E128"
    assert homeguard.CODE in load_errors()


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


def test_the_same_version_is_not_refused(two: tuple[Path, Path]) -> None:
    a, _ = two
    (a / "app" / "VERSION").write_text(__version__ + "\n", encoding="utf-8")
    v = homeguard.decide(a)
    assert (v.foreign, v.why) == (False, homeguard.WHY_SAME_VERSION)
    homeguard.require_own(a, "ns tune")
    first_start.guard(a / "config" / "runtime.json")     # goes on to the first-start check, raises nothing


def test_a_vouched_home_is_not_refused(two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    a, _ = two
    monkeypatch.setenv(nspaths.EXPLICIT_ENV, str(a))
    v = homeguard.decide(a)
    assert (v.foreign, v.why) == (False, homeguard.WHY_VOUCHED)
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
        first_start.guard(b / "config" / "runtime.json")


def test_a_home_the_code_named_itself_is_not_refused(two: tuple[Path, Path], tmp_path: Path) -> None:
    c = make_install(tmp_path / "NS-third", OLD)
    v = homeguard.decide(c)                 # NIGHTSHIFT_HOME names A, not C: not the window's accident
    assert (v.foreign, v.why) == (False, homeguard.WHY_NOT_BY_VARIABLE)


def test_a_program_in_no_install_is_never_refused(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = two
    monkeypatch.setattr(nspaths, "home_of_place", lambda *args, **kw: None)   # a checkout, the worker's copy
    v = homeguard.decide(a)
    assert (v.foreign, v.why) == (False, homeguard.WHY_NO_PLACE)
    first_start.guard(a / "config" / "runtime.json")


@pytest.mark.parametrize("how", ["missing", "empty", "not-text"])
def test_an_unreadable_version_counts_as_different(two: tuple[Path, Path], how: str) -> None:
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
    assert caught.value.code == "NS-E128"
    assert "its version could not be read" in str(caught.value) and str(f) in str(caught.value)


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
    without = homeguard.sentence(v, "ns tune")
    assert f"{a / 'venvs' / 'supervisor' / 'Scripts' / 'ns.exe'} tune" in without
    assert f"run {b / 'venvs' / 'supervisor' / 'Scripts' / 'ns.exe'} there" in without
    (a / "cmd").mkdir()
    (a / "cmd" / "ns.cmd").write_text("@echo off\n", encoding="utf-8")
    (a / "cmd" / "studio.cmd").write_text("@echo off\n", encoding="utf-8")
    assert f"{a / 'cmd' / 'ns.cmd'} schedule install" in homeguard.sentence(v, "ns schedule install")
    assert f"{a / 'cmd' / 'studio.cmd'} pause" in homeguard.sentence(v, "studio pause")
    assert str(a / "cmd" / "ns.cmd") in homeguard.sentence(v)         # a writer's refusal: no command known


def test_nothing_is_remembered_between_two_calls(two: tuple[Path, Path]) -> None:
    """Second run: the other install is updated to this version while this process lives."""
    a, _ = two
    assert homeguard.decide(a).foreign
    (a / "app" / "VERSION").write_text(__version__, encoding="utf-8")
    assert not homeguard.decide(a).foreign
    (a / "app" / "VERSION").write_text(OLD, encoding="utf-8")
    assert homeguard.decide(a).foreign


NS_REFUSED = [
    (["tune"], "ns tune"),
    (["restore", "nightshift-backup-x.zip"], "ns restore"),
    (["schedule", "install"], "ns schedule install"),
    (["schedule", "remove"], "ns schedule remove"),
    (["models", "get", "voicedesign"], "ns models get"),
]


@pytest.mark.parametrize(("argv", "command"), NS_REFUSED, ids=[c for _, c in NS_REFUSED])
def test_the_refusal_comes_before_any_side_effect(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, argv: list[str], command: str
) -> None:
    from ns_supervisor import cli, control, kitnights, models_get

    a, _ = two
    reached: list[str] = []

    def never(name: str) -> Callable[..., None]:
        def call(*args: object, **kw: object) -> None:
            reached.append(name)
            raise AssertionError(f"{name} was reached after a refusal was due")
        return call

    monkeypatch.setattr(cli, "_schedule_call", never("_schedule_call"))
    monkeypatch.setattr(cli, "_client", never("_client"))
    monkeypatch.setattr(control, "read_runtime", never("read_runtime"))
    monkeypatch.setattr(kitnights, "owner_chose", never("owner_chose"))
    monkeypatch.setattr(models_get, "get_model", never("get_model"))
    before = config_bytes(a)
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 1, result.output
    assert reached == []
    assert config_bytes(a) == before
    assert sorted(p.name for p in a.iterdir()) == ["app", "config"]      # no logs, no data, no run folder


STUDIO_REFUSED = [
    ["run"], ["run", "--daily"], ["run", "--resume", "j1"],
    ["channel", "add", "--slug", "s", "--name", "n", "--niche", "x"],
    ["channel", "edit", "s", "--set", "name=1"],
    ["voice", "design", "v", "--description", "d"],
    ["voice", "apply-pending"],
    ["pause", "s"], ["resume", "s", "--yes"],
]


@pytest.mark.parametrize("argv", STUDIO_REFUSED, ids=[" ".join(a[:2]) for a in STUDIO_REFUSED])
def test_the_studio_refuses_before_a_job_folder_or_the_database(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    from ns_studio import cli, jobs

    a, _ = two
    reached: list[str] = []

    def never(name: str) -> Callable[..., None]:
        def call(*args: object, **kw: object) -> None:
            reached.append(name)
            raise AssertionError(f"{name} was reached after a refusal was due")
        return call

    monkeypatch.setattr(cli, "job_gate", never("job_gate"))
    monkeypatch.setattr(jobs, "open_env", never("open_env"))
    monkeypatch.setattr(jobs, "load_stage_modules", never("load_stage_modules"))
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 1, result.output
    assert reached == []
    assert sorted(p.name for p in a.iterdir()) == ["app", "config"]


def test_reading_commands_of_the_supervisor_are_not_guarded(
    two: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ns tune --dry-run` is a read: it gets past the guard (and reaches the runtime reader)."""
    from ns_supervisor import cli, control

    reached: list[str] = []

    def read(paths: object) -> object:
        reached.append("read_runtime")
        raise NsError("NS-E204", "stop here: the read was reached")

    monkeypatch.setattr(control, "read_runtime", read)
    CliRunner().invoke(cli.app, ["tune", "--dry-run"])
    assert reached == ["read_runtime"]


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


GUARDED_COMMANDS = {
    "ns_supervisor/cli.py": ("_own", {
        "cmd_tune", "cmd_restore", "schedule_install", "schedule_remove", "models_get_cmd",
    }),
    "ns_studio/cli.py": ("own_home", {
        "run_cmd", "channel_add", "channel_edit", "voice_design", "voice_clone", "voice_apply_pending",
        "pause_cmd", "resume_cmd",
    }),
}


@pytest.mark.parametrize("rel", sorted(GUARDED_COMMANDS))
def test_every_command_of_the_card_calls_the_guard(rel: str) -> None:
    helper, wanted = GUARDED_COMMANDS[rel]
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    have = {
        n.name for n in tree.body
        if isinstance(n, ast.FunctionDef) and ("", helper) in _calls(n)
    }
    assert wanted <= have, sorted(wanted - have)
