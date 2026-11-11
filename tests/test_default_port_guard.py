"""tests/conftest.py refuses a connection to the product's default ports (8099, 8100, 8080) on loopback, and
fails the test that made it, by name, even when the product swallowed the refusal.

The guard is exercised the way it is used: in a pytest process of its own (this session's conftest copied
beside the probe tests), so the probes fail there without failing this session.

A product module that a test starts as a process of its own is outside that refusal (the conftest is not in
it). The last part of this file holds every test file to the rule by its text instead: such a file gives a
home an engine port of its own, or is listed with the reason its process asks no engine.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBES = '''
import asyncio
import socket
import threading
import urllib.request

import httpx
import pytest

import conftest


def swallow(call):
    try:
        call()
    except Exception:       # the product's own habit: an engine that does not answer is "down"
        pass


def test_raw_socket_to_the_engine_port():
    swallow(lambda: socket.create_connection(("127.0.0.1", 8099), timeout=1))


def test_httpx_to_the_dashboard_port():
    swallow(lambda: httpx.get("http://127.0.0.1:8100/status", timeout=1))


def test_urllib_to_localhost_story_model_port():
    swallow(lambda: urllib.request.urlopen("http://localhost:8080/health", timeout=1))


def test_asyncio_to_the_engine_port():
    swallow(lambda: asyncio.run(asyncio.open_connection("127.0.0.1", 8099)))


def test_a_thread_of_the_test_to_the_engine_port():
    def work():
        swallow(lambda: socket.create_connection(("127.0.0.1", 8099), timeout=1))

    t = threading.Thread(target=work)
    t.start()
    t.join()


def test_a_closed_port_of_its_own_is_refused_by_the_system_and_the_test_passes(closed_port):
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", closed_port), timeout=1)


def test_a_stand_in_server_on_its_own_port_is_reachable_and_the_test_passes():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        socket.create_connection(server.getsockname(), timeout=1).close()


def test_default_port_of_names_loopback_on_a_default_port_only():
    assert conftest.default_port_of("192.0.2.1", 8099) is None
    assert conftest.default_port_of("127.0.0.1", 9000) is None
    assert conftest.default_port_of("localhost", "8080") == 8080
    assert conftest.default_port_of("127.0.0.2", 8100) == 8100
    assert conftest.default_port_of("::1", 8099) == 8099
'''


def run_probes(tmp_path: Path) -> str:
    folder = tmp_path / "probe"
    folder.mkdir()
    conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    (folder / "conftest.py").write_text(conftest, encoding="utf-8")
    (folder / "test_probes.py").write_text(PROBES, encoding="utf-8")
    drop = ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST", "NIGHTSHIFT_HOME")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PYTHONPATH"] = str(ROOT)
    env["COLUMNS"] = "300"      # the failure text is read whole
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider",
         f"--basetemp={tmp_path / 'bt'}", "test_probes.py"],
        cwd=folder, env=env, capture_output=True, text=True, timeout=300,
    )
    return done.stdout + done.stderr


def outcome(output: str, name: str) -> str:
    for line in output.splitlines():
        for word in ("PASSED", "FAILED"):
            if line.startswith(f"{word} test_probes.py::{name}"):
                return word
    return "missing"


def test_a_connection_to_a_default_port_fails_the_test_that_made_it_even_when_the_product_swallows_it(
    tmp_path: Path,
) -> None:
    out = run_probes(tmp_path)
    for name, port, label in (
        ("test_raw_socket_to_the_engine_port", 8099, "the engine"),
        ("test_httpx_to_the_dashboard_port", 8100, "the dashboard"),
        ("test_urllib_to_localhost_story_model_port", 8080, "the story model"),
        ("test_asyncio_to_the_engine_port", 8099, "the engine"),
        ("test_a_thread_of_the_test_to_the_engine_port", 8099, "the engine"),
    ):
        assert outcome(out, name) == "FAILED", (name, out)
        said = f"test_probes.py::{name} connected to the product's default port {port} ({label})"
        assert said in out, (name, out)


def test_a_port_of_its_own_is_not_touched_by_the_guard(tmp_path: Path) -> None:
    out = run_probes(tmp_path)
    for name in (
        "test_a_closed_port_of_its_own_is_refused_by_the_system_and_the_test_passes",
        "test_a_stand_in_server_on_its_own_port_is_reachable_and_the_test_passes",
        "test_default_port_of_names_loopback_on_a_default_port_only",
    ):
        assert outcome(out, name) == "PASSED", (name, out)
    assert "5 failed, 3 passed" in out, out


# ---------------------------------------------------------------------------------------------------------
# a process of its own that a test starts: the conftest is not in it
# ---------------------------------------------------------------------------------------------------------
# The refusal above lives in the pytest process. A product program a test starts as a process of its own
# (`python -m ns_supervisor.cli verify --quick`) has no conftest: nothing refuses its connection, and it asks
# the ports its HOME names, which are the defaults unless the test wrote others into config/runtime.json.
# That is how tests/test_start_gate_routes.py (block D) reached the real engine of a PC that ran one: its
# servers had free ports, its home kept 8099, `ns verify --quick` asked 8099 before refusing, the real engine
# answered, and the command went on to verify with it. Nothing in this session saw the connection.
#
# A child cannot be held to the rule while it runs, so the test's text is, over the whole folder: a test file
# that starts a product module as a process of its own gives a home an engine port of its own, or is listed
# below with the reason no process of it asks the engine. What is read, and what is not:
# - "starts a product module": the file starts a real process (subprocess, asyncio's create_subprocess_*) and
#   holds an argv with an interpreter, `-m` and a product module (named there, or supplied from data while
#   the file names product modules as whole strings).
# - "an engine port of its own": `supervisor_port` is given a value other than 8099 somewhere in the file (a
#   keyword, a dict key, or JSON text).
# - It is judged per file, not per process: a file that gives one home a port and starts a second process on
#   another home passes.
# - Not read: the other ports a home names (`llama.port` and the rest: tests/test_start_gate_routes.py sets
#   all four); a `-c` program, a script or a driver under tests/ that the test wrote itself (every call it
#   makes is in the test's own text); a PowerShell or cmd child (the launch scripts ask the engine through
#   launchers/_common.ps1, on the port of the home's runtime.json, 8099 without one); a process the PRODUCT
#   starts while a test calls it in this process.
_MODULE = re.compile(
    r"(?:ns_common|ns_supervisor|ns_studio|ns_dashboard|ns_tts|installer|proofs|dev)(?:\.[A-Za-z_]\w*)+"
)
_PORT_IN_TEXT = re.compile(r"""["']supervisor_port["']\s*:\s*(?!8099\b)\S""")
_STARTS = {
    "subprocess": {"Popen", "run", "call", "check_call", "check_output"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
}


def _is_default(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and value.value == 8099


def product_processes(source: str) -> tuple[list[str], bool]:
    """(the product modules a test file starts as processes of their own, each with its line; whether the
    file gives a home an engine port of its own). The first is empty for a file that starts no process."""
    starts = own_port = False
    named: set[str] = set()                     # product modules named as whole strings anywhere in the file
    runs: list[tuple[int, str]] = []
    from_data: list[int] = []                   # `-m` followed by a name or a starred value, by line
    with warnings.catch_warnings():             # (a file's own string escapes would warn here, not there)
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                starts = starts or f.attr in _STARTS.get(f.value.id, ())
            own_port = own_port or any(
                k.arg == "supervisor_port" and not _is_default(k.value) for k in node.keywords
            )
        elif isinstance(node, ast.Dict):
            own_port = own_port or any(
                isinstance(k, ast.Constant) and k.value == "supervisor_port" and not _is_default(v)
                for k, v in zip(node.keys, node.values, strict=True)
            )
        elif isinstance(node, (ast.List, ast.Tuple)):
            words = [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
            at = words.index("-m") if "-m" in words else 0
            if at >= 1 and "-c" not in words[:at]:      # an interpreter before it, and no `-c` program
                module = words[at + 1] if at + 1 < len(words) else None
                if module is None:
                    from_data.append(node.lineno)
                elif isinstance(module, str) and _MODULE.fullmatch(module):
                    runs.append((node.lineno, f"-m {module}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _MODULE.fullmatch(node.value):
                named.add(node.value)
            own_port = own_port or bool(_PORT_IN_TEXT.search(node.value))
    if named:
        runs += [(n, f"-m with the module from data ({', '.join(sorted(named))})") for n in from_data]
    return ([f"line {n}: {what}" for n, what in sorted(runs)] if starts else []), own_port


#: Test files that start a product module as a process of its own and give no home an engine port, each with
#: the reason no process of it asks the engine. A file is put here only after each of its processes was read
#: to the point where it ends; "it passes" is not a reason (it passes on every PC where no engine runs).
#: These seven were also run on 2026-11-11 while a stand-in that answers like an engine listened on 8099 and
#: recorded every connection: none came (the first file skips without FFmpeg and was read only).
NO_ENGINE_ASKED: dict[str, str] = {
    "test_dashboard_studio_job_smoke.py":
        "every service is a fake (`fake: sorted(FAKEABLE)`) and no `supervisor_url` is passed, so "
        "`ns_dashboard.studio_job` builds no engine client (`_acquirer` returns None; the studio's own "
        "choice is then `DevAcquirer`)",
    "test_home_for_process.py":
        "`ns backup` copies files of the home; `ns_supervisor/backup.py` builds no engine client",
    "test_installer_stage1_args.py":
        "the argv with `-m installer.stage2.web` is the expected value: stage1.ps1's own argument list is "
        "handed to a script that echoes it, and -DryRun prints it; the setup server is never started",
    "test_installer_web_headless.py":
        "`--simulate` gives the setup server its fake effects (`fakes.fake_effects`: `engine_answers` says "
        "False without asking) and its wizard simulates every step",
    "test_no_real_machine.py":
        "`ns schedule install` and `ns schedule remove` end at the refusal of the Task Scheduler; "
        "`_schedule_call` builds no engine client",
    "test_scaffold.py":
        "`python -m ns_studio.cli` and `python -m ns_supervisor.cli` without a command print the version "
        "and end",
    "test_wizard_e2e.py":
        "the wizard child gets `--simulate-downloads` and the arguments `main(args(...))` gets in the same "
        "file, where it runs in this process under the refusal above; `--help` prints and ends",
}

THIS_SHAPE = """
def pending_home(tmp_path):
    paths = Paths(tmp_path / "home")
    save_runtime(paths, Runtime.default("cpu-dev").model_copy(update={"dev_mode": True}))
    return paths

def entries():
    return [("tts server", ["ns_tts.server", "--port", str(_free_port())]),
            ("ns verify", ["ns_supervisor.cli", "verify", "--quick"])]

def test_every_entry_refuses(tmp_path):
    paths = pending_home(tmp_path)
    for name, args in entries():
        subprocess.Popen([sys.executable, "-m", *args], env={"NIGHTSHIFT_HOME": str(paths.home)})
"""


def test_the_reading_sees_a_product_process_and_an_engine_port_of_its_own() -> None:
    """The shape that got through in block D, and each thing the reading must not take for it."""
    runs, own_port = product_processes(THIS_SHAPE)
    assert runs == ["line 14: -m with the module from data (ns_supervisor.cli, ns_tts.server)"]
    assert not own_port
    settings = '{"dev_mode": True}'
    fixed = THIS_SHAPE.replace(settings, '{"dev_mode": True, "supervisor_port": _free_port()}')
    assert product_processes(fixed) == (runs, True)
    assert product_processes(THIS_SHAPE.replace(settings, '{"supervisor_port": 8099}')) == (runs, False)
    named = "subprocess.run([str(py), '-m', 'ns_supervisor.cli', 'backup'])\n"
    assert product_processes(named) == (["line 1: -m ns_supervisor.cli"], False)
    assert product_processes(named + "Runtime(supervisor_port=port)\n")[1] is True
    assert product_processes(named + "Runtime(supervisor_port=8099)\n")[1] is False
    assert product_processes(named + """f.write_text('{"supervisor_port": 9}')\n""")[1] is True
    assert product_processes(named + """f.write_text('{"supervisor_port": 8099}')\n""")[1] is False
    # not a product process: no real start; a compared argv slice; a `-c` program with words after it;
    # a module that is not the product's
    assert product_processes("cmd = [py, '-m', 'ns_tts.server']\nassert svc.cmd == cmd\n") == ([], False)
    for harmless in (
        "assert seen[1:3] == ['-m', 'ns_supervisor.cli']",
        "argv = [py, '-c', 'import time; time.sleep(9)', '-m', 'ns_dashboard.server']",
        "argv = [py, '-m', 'pytest', 'tests/test_x.py']",
    ):
        assert product_processes(harmless + "\nsubprocess.Popen(argv)\n") == ([], False), harmless


def test_a_test_that_starts_a_product_process_gives_a_home_an_engine_port_of_its_own() -> None:
    wrong: list[str] = []
    for file in sorted((ROOT / "tests").glob("*.py")):
        runs, own_port = product_processes(file.read_text(encoding="utf-8"))
        listed = file.name in NO_ENGINE_ASKED
        if runs and not own_port and not listed:
            wrong.append(
                f"tests/{file.name} starts the product as a process of its own ({'; '.join(runs[:3])}) and "
                "gives no home an engine port of its own. That process has no conftest: with the default "
                "port it asks the real Nightshift engine of this PC whenever one runs. Write the home's "
                "config/runtime.json with `supervisor_port` (and the other ports) set to free ports, as "
                "`own_ports` in tests/test_start_gate_routes.py does; or, when no process of the file can "
                "ask the engine, list the file in NO_ENGINE_ASKED with the reason."
            )
        elif listed and (not runs or own_port):
            why = "names an engine port of its own now" if own_port else "starts no product module any more"
            wrong.append(f"tests/{file.name} is listed in NO_ENGINE_ASKED but {why}: take it off the list.")
    wrong += [
        f"NO_ENGINE_ASKED lists tests/{name}, which does not exist."
        for name in NO_ENGINE_ASKED if not (ROOT / "tests" / name).is_file()
    ]
    assert not wrong, "\n".join(wrong)
