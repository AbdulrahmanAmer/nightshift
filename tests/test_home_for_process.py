# ruff: noqa: E501
"""Which home a program works on when it is inside one install and NIGHTSHIFT_HOME names another folder
(card second-install-takeover, review @ 5ac9f00, blocker 1).

The rule (`ns_common.paths.Paths.from_env`): a NIGHTSHIFT_HOME that was set FOR this process is followed; one
that is only the user-level default gives way to the install the program is in. "Set for this process" is
known two ways: the starter vouched for it (`NS_HOME_EXPLICIT`, the same folder), or it differs from the
user-level variable. Under a test the user-level variable is `NS_TEST_USER_HOME`: the registry is never read.

The three calls the review named run here as they run in the product, each with a real interpreter that
lives in `<install>/venvs/studio/Scripts` (a venv made for this file), so `sys.executable` says "I am a
program of this install":

- setup's test render: `V01Samples.run` through the real `SubprocessRunner` (a stand-in for the `studio`
  program that asks the real `Paths.from_env` where its home is);
- a proof's helper: `dev.prove_ctx.Ctx.spawn` with that interpreter, the home under proof being another install;
- an owner: the real `ns backup` with `NIGHTSHIFT_HOME` typed for install B.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import ns_common.paths as paths_mod
from dev import prove_ctx
from installer.stage2.runner import RunResult, SubprocessRunner
from installer.stage2.steps.v01_samples import V01Samples, sample_dir
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import cli

ROOT = Path(__file__).resolve().parents[1]
# spelled out (and held equal to the product's names below), so that on a base without the rule these tests
# fail at what they assert and not at an import
HOME_ENV, EXPLICIT_ENV, TEST_USER_HOME_ENV = "NIGHTSHIFT_HOME", "NS_HOME_EXPLICIT", "NS_TEST_USER_HOME"
OURS = (HOME_ENV, EXPLICIT_ENV, TEST_USER_HOME_ENV)


def test_the_names_of_the_variables_are_the_products():
    assert (paths_mod.HOME_ENV, paths_mod.EXPLICIT_ENV, paths_mod.TEST_USER_HOME_ENV) == OURS
    assert paths_mod.home_env(Path("D:/x")) == {HOME_ENV: str(Path("D:/x")), EXPLICIT_ENV: str(Path("D:/x"))}


def install_at(folder: Path) -> Path:
    """A folder that counts as an install: it holds `config/runtime.json`."""
    (folder / "config" / "channels").mkdir(parents=True, exist_ok=True)
    (folder / "config" / "runtime.json").write_text("{}", encoding="utf-8")
    (folder / "app").mkdir(exist_ok=True)
    (folder / "app" / "VERSION").write_text("0.1.15\n", encoding="ascii")
    (folder / "config" / "channels" / f"{folder.name.lower()}-only.yaml").write_text("x: 1\n", encoding="utf-8")
    return folder


@pytest.fixture(scope="module")
def pc(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """Two installs on one PC. A (`<root>/home`) has a real venv `venvs/studio`; B is another folder."""
    root = tmp_path_factory.mktemp("two-installs")
    a = install_at(root / "home")           # `make_test_context(root)` makes its home at `<root>/home`
    b = install_at(root / "B")
    venv = a / "venvs" / "studio"
    made = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)], capture_output=True, text=True, timeout=600,
    )
    assert made.returncode == 0, made.stdout + made.stderr
    python = venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"
    assert python.is_file()
    place = getattr(paths_mod, "home_of_place", None)      # (a base without the place rule has none)
    assert place is None or place(ROOT, str(python)) == a   # what the child will find about itself
    return SimpleNamespace(root=root, a=a, b=b, python=python)


def child_env(**extra: str) -> dict[str, str]:
    """This process's environment without the three home variables, with the product (and what it imports)
    importable by a bare venv, then `extra`."""
    env = {k: v for k, v in os.environ.items() if k not in OURS}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), sysconfig.get_path("purelib")])
    env["PYTHONUTF8"] = "1"
    env.update(extra)
    return env


# ---------------------------------------------------------------------------------------------------------
# (a) setup's test render
# ---------------------------------------------------------------------------------------------------------
STAND_IN_STUDIO = '''
"""Stands in for the `studio` program of an install: it asks the real ns_common where its home is."""
import sqlite3
import sys
import wave

from ns_common.paths import Paths


def main(argv):
    paths = Paths.from_env()
    paths.ensure()
    if argv[:2] == ["voice", "preview"]:
        with wave.open(argv[argv.index("--out") + 1], "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(bytes(8000))
        return 0
    if argv[:2] == ["run", "--fake-services"]:
        db = sqlite3.connect(paths.db_path)
        db.execute("create table if not exists stories (title text)")
        db.execute("insert into stories values ('the fake story')")
        db.commit()
        db.close()
        video = paths.outbox / "channel" / "the-fake-story.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"not a real video")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def stories(home: Path) -> list[str]:
    db_file = home / "data" / "studio.db"
    if not db_file.is_file():
        return []
    db = sqlite3.connect(db_file)
    try:
        return [row[0] for row in db.execute("select title from stories")]
    except sqlite3.OperationalError:        # no such table
        return []
    finally:
        db.close()


class SetupRunner:
    """The real runner for `studio`; a stand-in for FFmpeg, which looks at the scratch home when setup
    asks for the frame (setup removes that folder when it is done)."""

    def __init__(self, scratch: Path) -> None:
        self.real = SubprocessRunner()
        self.scratch = scratch
        self.studio_calls: list[dict[str, Any]] = []
        self.scratch_stories: list[str] | None = None
        self.scratch_videos: list[str] = []

    def run(self, argv: Any, **kw: Any) -> RunResult:
        program = Path(str(argv[0])).stem.lower()
        if program == "ffmpeg":
            self.scratch_stories = stories(self.scratch)
            self.scratch_videos = [p.name for p in (self.scratch / "outbox").rglob("*.mp4")]
            Path(str(argv[-1])).write_bytes(b"jpeg")
            return RunResult(0)
        if program == "ffprobe":
            return RunResult(0, "1.5\n")
        self.studio_calls.append({"argv": [str(a) for a in argv], "env": dict(kw.get("env") or {})})
        return self.real.run(argv, **kw)


def test_setups_test_render_writes_its_story_into_the_scratch_home_and_not_the_owners(
    pc: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    program = pc.root / "program"           # setup's working folder for `studio` (`cwd=ctx.app_path`)
    (program / "ns_studio").mkdir(parents=True, exist_ok=True)
    (program / "ns_studio" / "__init__.py").write_text("", encoding="utf-8")
    (program / "ns_studio" / "cli.py").write_text(STAND_IN_STUDIO, encoding="utf-8")
    # setup's own process, as on a PC whose default names this install: the test render's child inherits
    # everything but what `_render` hands it
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv(HOME_ENV, str(pc.a))
    monkeypatch.setenv(TEST_USER_HOME_ENV, str(pc.a))
    monkeypatch.delenv(EXPLICIT_ENV, raising=False)
    scratch = pc.a / "cache" / "setup-test" / "home"
    runner = SetupRunner(scratch)
    ctx = make_test_context(
        pc.root, ui=FakeUi(), raw_answers=example_answers(), resolve=True, simulate=False, app_path=program,
        runner=runner,
    )
    assert ctx.paths.home == pc.a and sample_dir(ctx) / "home" == scratch
    said = ""
    try:
        V01Samples().run(ctx)
    except NsError as e:                    # what setup shows: "test render: no video written"
        said = e.detail

    render = next(c for c in runner.studio_calls if "run" in c["argv"])
    assert render["argv"][0] == str(pc.python) and render["argv"][1:3] == ["-m", "ns_studio.cli"]   # A's own program
    assert render["env"][HOME_ENV] == str(scratch)
    assert runner.scratch_stories == ["the fake story"]             # the scratch home got the story
    assert runner.scratch_videos == ["the-fake-story.mp4"]
    assert stories(pc.a) == []                                      # the owner's database: untouched
    assert not any((pc.a / "outbox").rglob("*.mp4"))                            # and the owner's outbox
    assert said == "" and ctx.flag("samples") == "done"
    assert (sample_dir(ctx) / "render-test.mp4").read_bytes() == b"not a real video"
    assert not scratch.exists()             # setup removed its scratch home


# ---------------------------------------------------------------------------------------------------------
# (b) a proof's helper, started with the Python of another install than the one under proof
# ---------------------------------------------------------------------------------------------------------
def test_a_proofs_helper_works_on_the_home_under_proof_also_with_another_installs_python(
    pc: SimpleNamespace, tmp_path: Path
):
    helper = tmp_path / "helper.py"
    helper.write_text(
        "from ns_common.paths import Paths\nprint('HOME=' + str(Paths.from_env().home))\n", encoding="utf-8"
    )
    # the hard row: the PC's default names the home under proof too, so nothing but the marker tells the
    # helper that the value was meant
    env = child_env(**{TEST_USER_HOME_ENV: str(pc.b), HOME_ENV: str(pc.a)})
    ctx = prove_ctx.Ctx(
        home=pc.b, info=prove_ctx.read_install(pc.b), scratch=tmp_path, proof_id="t", timeout_s=120, env=env,
    )
    try:
        proc = ctx.spawn([str(pc.python), str(helper)], name="helper")     # what `[sys.executable, ...]` is
        assert proc.wait(timeout=120) == 0, (tmp_path / "helper.log").read_text(encoding="utf-8", errors="replace")
    finally:
        ctx.close()
    said = (tmp_path / "helper.log").read_text(encoding="utf-8", errors="replace")
    assert f"HOME={pc.b}" in said.splitlines(), said
    assert "Nightshift: using" not in said                          # the runner vouched: nothing to say
    assert ctx._env[EXPLICIT_ENV] == str(pc.b) == ctx._env[HOME_ENV]   # prove_ctx spells the name out


# ---------------------------------------------------------------------------------------------------------
# (c) an owner in a terminal, with install A's `ns`
# ---------------------------------------------------------------------------------------------------------
def ns_backup(pc: SimpleNamespace, out: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    done = subprocess.run(
        [str(pc.python), "-m", "ns_supervisor.cli", "backup", "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, env=child_env(**env),
        cwd=str(pc.root),
    )
    names = zipfile.ZipFile(out).namelist() if out.is_file() else []
    return done, names


def test_an_owner_who_sets_the_variable_in_a_window_gets_that_install_and_is_told(pc: SimpleNamespace, tmp_path: Path):
    """`$env:NIGHTSHIFT_HOME = '<B>'` in a terminal, then install A's own `ns backup`, on a PC whose default
    is A: the backup is B's."""
    done, names = ns_backup(pc, tmp_path / "typed.zip", **{HOME_ENV: str(pc.b), TEST_USER_HOME_ENV: str(pc.a)})
    assert done.returncode == 0, done.stdout + done.stderr
    assert "config/channels/b-only.yaml" in names and "config/channels/home-only.yaml" not in names
    told = [ln for ln in done.stderr.splitlines() if ln.startswith("Nightshift: using")]
    assert told == [
        f"Nightshift: using {pc.b}: NIGHTSHIFT_HOME names it in this window (it is not this PC's default), "
        f"and this program is in the install at {pc.a}. If you did not set it, this window was opened before "
        "the default changed: open a new one."
    ]


def test_a_variable_that_is_only_the_pcs_default_never_sends_an_installs_program_elsewhere(
    pc: SimpleNamespace, tmp_path: Path
):
    """A fresh terminal on a PC whose default names B, and install A's own `ns backup`: the backup is A's.
    The same holds for an owner who typed the default's own value: the two cannot be told apart, and the
    line says how to reach the other install."""
    done, names = ns_backup(pc, tmp_path / "default.zip", **{HOME_ENV: str(pc.b), TEST_USER_HOME_ENV: str(pc.b)})
    assert done.returncode == 0, done.stdout + done.stderr
    assert "config/channels/home-only.yaml" in names and "config/channels/b-only.yaml" not in names
    told = [ln for ln in done.stderr.splitlines() if ln.startswith("Nightshift: using")]
    assert told == [
        f"Nightshift: using the install in {pc.a} (this program is in it); NIGHTSHIFT_HOME names {pc.b}, "
        "which is this PC's default and was not set in this window. To work on that folder, start its own "
        "shortcuts or programs."
    ]


# ---------------------------------------------------------------------------------------------------------
# the table, in this process: a program of install A (`home_of_place` says so)
# ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def in_a(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    a = install_at(tmp_path / "A")
    b = install_at(tmp_path / "B")
    plain = tmp_path / "not-an-install"
    plain.mkdir()
    monkeypatch.setattr(paths_mod, "repo_root", lambda: a / "app")
    monkeypatch.setattr(paths_mod, "_said_place", [], raising=False)
    for name in OURS:
        monkeypatch.delenv(name, raising=False)

    def registry() -> tuple[bool, str | None]:
        raise AssertionError("a test run read the real registry")

    monkeypatch.setattr(paths_mod, "_read_user_variable", registry, raising=False)
    return SimpleNamespace(a=a, b=b, plain=plain)


#: (the process's NIGHTSHIFT_HOME, the user-level one, the marker) -> (the home used, the line's first words)
ROWS = [
    # a Desktop shortcut, the nightly task, the engine starting a child: the variable is the install's own
    ("a", None, None, "a", None),
    ("a", "b", None, "a", None),
    ("a", "plain", None, "a", None),
    # a fresh terminal: only the PC's default
    ("b", "b", None, "a", "Nightshift: using the install in"),
    ("plain", "plain", None, "a", "Nightshift: using the install in"),
    # typed in the window (or set by whatever started this process)
    ("b", None, None, "b", "Nightshift: using {b}: NIGHTSHIFT_HOME names it in this window"),
    ("b", "a", None, "b", "Nightshift: using {b}: NIGHTSHIFT_HOME names it in this window"),
    ("b", "plain", None, "b", "Nightshift: using {b}: NIGHTSHIFT_HOME names it in this window"),
    ("plain", "a", None, "plain", "Nightshift: using {plain}: NIGHTSHIFT_HOME names it in this window"),
    # a starter that vouched (setup's test render, a proof, `ns update --home`): whatever the default is
    ("b", "b", "b", "b", None),
    ("b", None, "b", "b", None),
    ("plain", "a", "plain", "plain", None),
    # a marker left over from another home vouches for nothing
    ("b", "b", "a", "a", "Nightshift: using the install in"),
    ("b", "a", "plain", "b", "Nightshift: using {b}: NIGHTSHIFT_HOME names it in this window"),
]


@pytest.mark.parametrize(("value", "user", "marker", "used", "line"), ROWS)
def test_the_table_for_a_program_inside_an_install(
    in_a: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    value: str, user: str | None, marker: str | None, used: str, line: str | None,
):
    folder = {"a": in_a.a, "b": in_a.b, "plain": in_a.plain}
    monkeypatch.setenv(HOME_ENV, str(folder[value]))
    if user:
        monkeypatch.setenv(TEST_USER_HOME_ENV, str(folder[user]))
    if marker:
        monkeypatch.setenv(EXPLICIT_ENV, str(folder[marker]))
    assert Paths.from_env().home == folder[used]
    assert Paths.from_env().home == folder[used]
    err = capsys.readouterr().err
    if line is None:
        assert err == ""
    else:
        assert err.startswith(line.format(b=in_a.b, plain=in_a.plain)) and err.count("Nightshift: using") == 1


def test_when_windows_does_not_say_the_program_stays_in_its_own_install_and_says_so(
    in_a: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(paths_mod, "user_home_variable", lambda: (False, None), raising=False)
    monkeypatch.setenv(HOME_ENV, str(in_a.b))
    assert Paths.from_env().home == in_a.a
    assert capsys.readouterr().err == (
        f"Nightshift: using the install in {in_a.a} (this program is in it); NIGHTSHIFT_HOME names {in_a.b}, "
        "and Windows did not say whether that was set in this window. To work on that folder, start its own "
        "shortcuts or programs.\n"
    )
    monkeypatch.setenv(EXPLICIT_ENV, str(in_a.b))                   # a starter's word needs no registry
    assert Paths.from_env().home == in_a.b


def test_a_program_in_no_install_and_a_handed_environment_follow_the_variable_as_before(
    in_a: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv(TEST_USER_HOME_ENV, str(in_a.b))
    assert Paths.from_env({HOME_ENV: str(in_a.b)}).home == in_a.b   # somebody else's process: not second-guessed
    monkeypatch.setattr(paths_mod, "repo_root", lambda: ROOT)       # a checkout, a Python of its own
    monkeypatch.setenv(HOME_ENV, str(in_a.b))
    assert Paths.from_env().home == in_a.b                          # the default decides: no install to prefer
    monkeypatch.delenv(HOME_ENV)
    monkeypatch.setattr(paths_mod, "repo_root", lambda: in_a.a / "app")
    monkeypatch.setenv("LOCALAPPDATA", str(in_a.plain))
    assert Paths.from_env().home == in_a.plain / "Nightshift"       # no variable: as before (NS-E123's case)
    assert capsys.readouterr().err == ""


def test_a_home_a_test_sets_is_its_own_even_on_an_installs_python(in_a: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`monkeypatch.setenv(NIGHTSHIFT_HOME, tmp)` under a pytest that runs on an install's venv: the test's
    folder is used (and the registry is not read: `in_a` fails the test if it is)."""
    mine = tmp_path / "home-of-the-test"
    monkeypatch.setenv(HOME_ENV, str(mine))
    assert Paths.from_env().home == mine


def test_ns_update_home_names_the_home_for_the_worker_and_its_children(in_a: SimpleNamespace, monkeypatch: pytest.MonkeyPatch):
    """`ns update --home <B>` from a program of install A while the PC's default names B: B, and the two
    variables stay in the process's environment for what the worker starts."""
    seen: list[Path] = []
    monkeypatch.setenv(HOME_ENV, str(in_a.b))                       # also restores what the command sets
    monkeypatch.setenv(EXPLICIT_ENV, "")
    monkeypatch.setenv(TEST_USER_HOME_ENV, str(in_a.b))
    assert Paths.from_env().home == in_a.a                          # without the option: the default gives way
    monkeypatch.setattr(cli.control, "app_path", lambda paths: seen.append(paths.home) or paths.home / "app")
    monkeypatch.setattr(cli.update, "current_version", lambda _app: "0.0.0")
    monkeypatch.setattr(cli, "_update_check", lambda _current: None)
    done = CliRunner().invoke(cli.app, ["update", "--check", "--home", str(in_a.b)])
    assert done.exit_code == 0, done.output
    assert seen == [in_a.b]
    assert {k: os.environ.get(k) for k in (HOME_ENV, EXPLICIT_ENV)} == {HOME_ENV: str(in_a.b), EXPLICIT_ENV: str(in_a.b)}


# ---------------------------------------------------------------------------------------------------------
# reading the user-level variable
# ---------------------------------------------------------------------------------------------------------
class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ, REG_EXPAND_SZ = 1, 2

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.opened: list[tuple[Any, ...]] = []

    def OpenKey(self, *args: Any) -> Any:  # noqa: N802 - winreg's name
        self.opened.append(args)
        return self

    def __enter__(self) -> FakeWinreg:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def QueryValueEx(self, _key: Any, name: str) -> tuple[Any, int]:  # noqa: N802
        assert name == "NIGHTSHIFT_HOME"
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    @staticmethod
    def ExpandEnvironmentStrings(text: str) -> str:  # noqa: N802
        return text.replace("%DRIVE%", "D:")


@pytest.mark.skipif(sys.platform != "win32", reason="the user-level variable is a Windows thing")
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (("D:\\Nightshift", 1), (True, "D:\\Nightshift")),
        (("%DRIVE%\\Nightshift", 2), (True, "D:\\Nightshift")),
        (("", 1), (True, None)),
        (FileNotFoundError(2, "no such value"), (True, None)),      # not set for this user
        (PermissionError(5, "access denied"), (False, None)),       # Windows did not say
        ((b"\x00", 3), (False, None)),                              # not text
    ],
)
def test_the_user_variable_is_read_without_a_write_and_never_fails_the_process(
    monkeypatch: pytest.MonkeyPatch, answer: Any, expected: tuple[bool, str | None]
):
    fake = FakeWinreg(answer)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert paths_mod._read_user_variable() == expected
    assert fake.opened == [("HKCU", "Environment")]                 # winreg's default access: read


def test_the_user_variable_is_asked_once_per_process_and_never_under_a_test(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []

    def read() -> tuple[bool, str | None]:
        calls.append(1)
        return True, "D:\\Nightshift"

    monkeypatch.setattr(paths_mod, "_user_variable", [])
    assert paths_mod._user_variable_once(read) == (True, "D:\\Nightshift") == paths_mod._user_variable_once(read)
    assert calls == [1]
    # a test run: what the test says, and "not set" when it says nothing
    monkeypatch.setattr(paths_mod, "_read_user_variable", lambda: pytest.fail("a test run read the registry"))
    monkeypatch.delenv(TEST_USER_HOME_ENV, raising=False)
    assert paths_mod.user_home_variable() == (True, None)
    monkeypatch.setenv(TEST_USER_HOME_ENV, "E:\\Other")
    assert paths_mod.user_home_variable() == (True, "E:\\Other")
