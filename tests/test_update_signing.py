"""Card update-signing: an update package installs only when the release key signed it.

`<zip>.sig` (Ed25519 over the zip's name, its VERSION and its SHA256) is checked by `update.verify_package`
against the keys the INSTALLED program trusts, before anything is stopped or changed, on every route. The
key of these tests is the TEST key (`tests/fixtures/release_test_key.txt`), which an install trusts only
while it reads its releases from a test release source; a foreign key is made here. New modules are looked up,
and a base without the signer gets a `.sig` of the right shape, so on the base every test fails at an
assertion (the old code installs what it should refuse).
"""
# Dense assertions with their reasons beside them: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import ast
import base64
import hashlib
import importlib
import json
import runpy
import shutil
import sys
import threading
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from ns_common import rollback_intent
from ns_common.config import save_runtime
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor import cli, releases, update, update_job
from ns_supervisor.procs import UNDER_ENGINE_ENV
from tests import frozen_update_check_0_1_14 as frozen
from tests.test_ns_update import Env, make_app
from tests.test_release_source import ORIGIN, Net
from tests.test_update_handover import Hand, on_disk, point_home_at


def _module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


sigcheck = _module("ns_common.sigcheck")
release_keys = _module("ns_common.release_keys")
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
KEY_LINES = (FIXTURES / "release_test_key.txt").read_text(encoding="ascii").splitlines()
TEST_SEED = base64.b64decode(next(ln for ln in KEY_LINES if ln and not ln.startswith("#")))
FOREIGN_SEED = hashlib.sha256(b"a foreign key, made in this test").digest()
VAR = "NS_RELEASE_SOURCE"
SOURCE = f"{ORIGIN}/repos/x/releases/latest"  # names this PC; nothing listens there (`Net` answers)
NOTE = "This version of Nightshift does not check who made an update yet"
REAL: dict[str, Any] = {"dev_mode": False, "tts": {"engine": "qwentts", "device": "cuda"}}
runner = CliRunner()


@pytest.fixture(autouse=True)
def no_pinned_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test states the keys it means: none pinned (as before the owner's step) unless it pins one."""
    monkeypatch.setattr(update, "RENAME_PAUSE_S", 0.05)
    if release_keys is not None:
        monkeypatch.setattr(release_keys, "TRUSTED", {})


def key_of(seed: bytes) -> tuple[str, str]:
    """(key id, base64 public key) of a private key, as `release_keys` lists it."""
    public = sigcheck.public_key(seed)
    return sigcheck.key_id(public), base64.b64encode(public).decode("ascii")


def reseal(z: Path) -> None:
    """The `.sha256` of what the zip holds now (whoever can change a package can do this too)."""
    digest = hashlib.sha256(z.read_bytes()).hexdigest()
    z.with_name(z.name + ".sha256").write_text(f"{digest}  {z.name}\n", encoding="ascii")


def package(
    folder: Path, version: str = "0.1.16", *, name: str | None = None, payload: str = "NEW",
    members: dict[str, str] | None = None,
) -> Path:
    """`Nightshift-Update-<version>.zip` and its `.sha256`, as `dev/make_bundle.ps1` leaves them."""
    folder.mkdir(parents=True, exist_ok=True)
    z = folder / (name or f"Nightshift-Update-{version}.zip")
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("VERSION", version + "\n")
        zf.writestr("ns_common/x.py", payload)
        for member, text in (members or {}).items():
            zf.writestr(member, text)
    reseal(z)
    return z


def sig_of(z: Path) -> Path:
    return z.with_name(z.name + ".sig")


def sign(z: Path, seed: bytes = TEST_SEED) -> Path:
    """`<zip>.sig` made with `seed`."""
    if sigcheck is None:  # the base has no signer and reads no .sig: a file of the right shape
        blank = base64.b64encode(bytes(64)).decode("ascii")
        sig_of(z).write_text(f"alg: ed25519\nkey: {'0' * 16}\nsig: {blank}\n", encoding="ascii")
        return sig_of(z)
    digest = hashlib.sha256(z.read_bytes()).hexdigest()
    signed = sigcheck.message(z.name, sigcheck.package_version(z)[0], digest)
    text = sigcheck.sig_text(sigcheck.public_key(seed), sigcheck.sign(seed, signed))
    sig_of(z).write_bytes(text.encode("ascii"))
    return sig_of(z)


def install_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, runtime: dict[str, Any] | None = None,
    source: str | None = SOURCE, port: int | None = None,
) -> Paths:
    """An install home. As made here (a dev install with the variable naming this PC) it reads its releases
    from a test source, so it trusts the TEST key; `runtime` and `source` make the other installs."""
    paths = Paths(tmp_path / "home")
    paths.ensure()
    if port is None:
        data = {"dev_mode": True} if runtime is None else runtime
        paths.runtime_file.write_text(json.dumps(data), encoding="utf-8")
    else:  # a route through `ns`: the engine it asks is at a port nothing listens on
        rt = Runtime.default("gpu")
        rt.dev_mode, rt.supervisor_port = True, port
        save_runtime(paths, rt)
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(paths.home))
    if source is None:
        monkeypatch.delenv(VAR, raising=False)
    else:
        monkeypatch.setenv(VAR, source)
    return paths


def attempt(z: Path, app: Path, paths: Paths, env: Env | None = None) -> tuple[NsError | None, Env, list[str]]:
    """`update.update_app` as the worker calls it: (its refusal or None, what it did, what it said)."""
    e, said = env or Env(), []
    made = e.env()
    made.say = said.append
    try:
        update.update_app(z, app, made, paths=paths)
    except NsError as err:
        return err, e, said
    return None, e, said


def refused(z: Path, app: Path, paths: Paths, code: str = "NS-E707") -> NsError:
    err, e, _said = attempt(z, app, paths)
    assert err is not None, f"{z.name} was installed"
    assert err.code == code and e.calls == [], (err, e.calls)  # refused before anything was stopped
    return err


def version_in(app: Path) -> str:
    return (app / "VERSION").read_text(encoding="utf-8").strip()


def flip(data: bytes, at: int) -> bytes:
    return data[:at] + bytes([data[at] ^ 1]) + data[at + 1 :]


def dashboard(paths: Paths) -> Any:
    from ns_dashboard.handlers import Dashboard, build_state
    from ns_dashboard.supervisor_link import SupervisorLink

    return Dashboard(build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)))


# ------------------------------------------------------------------------------------------ the verifier
def test_the_verifier_passes_rfc8032_vectors() -> None:
    assert sigcheck is not None, "ns_common/sigcheck.py is missing"
    vectors = json.loads((FIXTURES / "rfc8032_vectors.json").read_text(encoding="ascii"))["vectors"]
    assert [v["test"] for v in vectors] == ["1", "2", "3", "1024"]
    for v in vectors:
        secret, public, msg, sig = (bytes.fromhex(v[k]) for k in ("secret", "public", "message", "signature"))
        assert sigcheck.verify(public, msg, sig), v["test"]
        # the reference signer of the tests and the proofs gives the RFC's own bytes
        assert sigcheck.public_key(secret) == public and sigcheck.sign(secret, msg) == sig, v["test"]
        changed = flip(msg, len(msg) // 2) if msg else b"\x00"
        assert not sigcheck.verify(public, changed, sig) and not sigcheck.verify(public, msg + b"\x00", sig)
        assert not sigcheck.verify(public, msg, flip(sig, 3)) and not sigcheck.verify(public, msg, flip(sig, 40))
        assert not sigcheck.verify(flip(public, 7), msg, sig)
        assert not sigcheck.verify(public[:31], msg, sig) and not sigcheck.verify(public + b"\x00", msg, sig)
        assert not sigcheck.verify(public, msg, sig[:63]) and not sigcheck.verify(public, msg, sig + b"\x00")
        # the same signature with the group order added to S (its malleable twin) is not one
        twin = int.from_bytes(sig[32:], "little") + 2**252 + 27742317777372353535851937790883648493
        assert not sigcheck.verify(public, msg, sig[:32] + twin.to_bytes(32, "little"))


def test_the_key_lists_are_well_formed_and_the_verifier_needs_no_wheel(tmp_path: Path) -> None:
    assert sigcheck is not None and release_keys is not None, "the verifier or the key list is missing"
    listed = runpy.run_path(str(ROOT / "ns_common" / "release_keys.py"))  # the file, not this run's patch
    for keys in (listed["TRUSTED"], listed["TEST_KEYS"]):
        for key, public in keys.items():
            assert sigcheck.key_id(base64.b64decode(public, validate=True)) == key
    assert key_of(TEST_SEED)[0] in listed["TEST_KEYS"] and not set(listed["TEST_KEYS"]) & set(listed["TRUSTED"])
    for name in ("sigcheck", "release_keys"):  # it runs in the OLD program's updater and in a bare release job
        tree = ast.parse((ROOT / "ns_common" / f"{name}.py").read_text(encoding="utf-8"))
        mods = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        mods += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        assert all(m.split(".")[0] in sys.stdlib_module_names for m in mods), (name, mods)
    raw = sign(package(tmp_path)).read_bytes()  # the .sig: three ASCII lines, and nothing else is one
    key, sig = sigcheck.parse_sig(raw)
    assert key == key_of(TEST_SEED)[0] and len(sig) == 64 and raw.count(b"\n") == 3
    for bad in (b"", raw + b"x: y\n", raw.replace(b"ed25519", b"ed448"), raw[:-10], raw.replace(b"key: ", b"key:  ")):
        with pytest.raises(ValueError):
            sigcheck.parse_sig(bad)


# --------------------------------------------------------------------------------- the check, G2 to G6
def test_a_signed_package_is_accepted_and_the_message_binds_name_version_and_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, app = install_home(tmp_path, monkeypatch), make_app(tmp_path)
    z = package(tmp_path / "dl")
    sig = sign(z)
    renamed = tmp_path / "renamed" / "Nightshift-Update-0.1.17.zip"  # the same bytes and .sig, the next name
    renamed.parent.mkdir()
    shutil.copy(z, renamed)
    reseal(renamed)
    shutil.copy(sig, sig_of(renamed))
    err = refused(renamed, app, paths)
    assert "does not match" in err.detail and "renamed" in err.detail and "Nothing was changed" in err.detail
    other = package(tmp_path / "other", "0.1.17", name=z.name)  # another VERSION under the signed name
    shutil.copy(sig, sig_of(other))
    assert "does not match" in refused(other, app, paths).detail
    err, e, said = attempt(z, app, paths)
    assert err is None and e.calls[0] == "stop" and version_in(app) == "0.1.16"
    told = [line for line in said if "signed with" in line]  # one line of the update log says who signed it
    assert len(told) == 1 and "TEST key" in told[0] and key_of(TEST_SEED)[0] in told[0], said
    assert not any(NOTE in line for line in said)


def test_a_changed_byte_is_refused_before_anything_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, app = install_home(tmp_path, monkeypatch), make_app(tmp_path)
    prev = app.with_name(app.name + ".prev")
    prev.mkdir()
    (prev / "VERSION").write_text("0.0.9\n", encoding="utf-8")
    (paths.venvs / "supervisor").mkdir(parents=True)
    (paths.venvs / "supervisor" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    (paths.config / "schedule.json").write_text('{"time": "03:00"}', encoding="utf-8")
    state = update_job.status_file(paths)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"state": "done", "message": "the update before this one"}', encoding="utf-8")
    z = package(tmp_path / "dl")
    sign(z)
    package(tmp_path / "dl", payload="NEX")  # a forged release: one byte of the zip, and its .sha256 remade
    before = (on_disk(app, paths), state.read_bytes())
    for _run in (1, 2):  # the second run (after a kill, or a retry) answers the same and still changes nothing
        err = refused(z, app, paths)
        assert "does not match" in err.detail and "Nothing was changed" in err.detail
        assert "download it again from the release page" in err.detail
        assert (on_disk(app, paths), state.read_bytes()) == before


def test_a_missing_sig_is_refused_with_the_file_to_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, app = install_home(tmp_path, monkeypatch), make_app(tmp_path)
    z = package(tmp_path / "dl")
    err = refused(z, app, paths)
    assert f"{z.name}.sig is missing, so Nightshift cannot tell who made this package." in err.detail
    assert "Nothing was changed. Download the .zip, .sha256 and .sig of the same release" in err.detail
    assert ".sha256 and .sig" in err.definition.hint and "Nothing was changed" in err.definition.hint
    sign(z)  # failed, then retried with the file it named: the same package installs
    err, e, _said = attempt(z, app, paths)
    assert err is None and e.calls[0] == "stop" and version_in(app) == "0.1.16"


def test_a_foreign_key_is_refused_with_its_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, app = install_home(tmp_path, monkeypatch), make_app(tmp_path)
    bad = package(tmp_path / "dl")
    sign(bad, FOREIGN_SEED)
    good = package(tmp_path / "dl", "0.1.17")
    sign(good)
    err = refused(bad, app, paths)
    assert "is signed with a key this version of Nightshift does not know" in err.detail
    assert key_of(FOREIGN_SEED)[0] in err.detail and "Do not install it" in err.detail
    assert "INSTALL.cmd" in err.detail  # the way over a replaced release key
    assert attempt(good, app, paths)[0] is None and version_in(app) == "0.1.17"  # the good one beside it installs


def test_a_sig_that_cannot_be_read_or_is_not_the_format_is_refused_in_its_own_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, app = install_home(tmp_path, monkeypatch), make_app(tmp_path)
    z = package(tmp_path / "dl")
    good = sign(z).read_bytes()
    for bad in (b"", good + b"more: lines\n", b"\xff\xfe" + good, good * 20):  # empty, a 4th line, not ASCII, long
        sig_of(z).write_bytes(bad)
        err = refused(z, app, paths)
        assert f"{z.name}.sig is not a Nightshift signature file" in err.detail and "all three" in err.detail
    sig_of(z).unlink()
    sig_of(z).mkdir()  # the name is there and cannot be read as a file
    err = refused(z, app, paths)
    assert f"{z.name}.sig cannot be read" in err.detail and "Nothing was changed" in err.detail


def test_a_program_with_no_key_yet_accepts_sha256_only_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = install_home(tmp_path, monkeypatch, runtime=REAL, source=None)  # no pinned key, no test source
    app = make_app(tmp_path)
    point_home_at(paths, app)
    z = package(tmp_path / "dl")  # no .sig: what a candidate published before the key exists looks like
    hand = Hand(monkeypatch)
    r = runner.invoke(cli.app, ["update", str(z)], env={"NIGHTSHIFT_HOME": str(paths.home)})
    assert r.exit_code == 0 and len(hand.workers) == 1 and NOTE not in r.output  # the terminal's check is silent
    dash = dashboard(paths)
    assert dash.library.update_state().get("unsigned_note") == NOTE  # Settings > Updates, from the running version
    err, e, said = attempt(z, app, paths)
    assert err is None and e.calls[0] == "stop" and version_in(app) == "0.1.16"
    told = [line for line in said if NOTE in line]  # once per update, in the worker's log
    assert len(told) == 1 and ".sha256" in told[0] and not any("signed with" in line for line in said), said
    stale = package(tmp_path / "dl", "0.1.17")
    sign(stale, FOREIGN_SEED)  # a .sig nobody here can judge changes nothing while no key is trusted
    assert attempt(stale, app, paths)[0] is None
    monkeypatch.setattr(release_keys, "TRUSTED", dict([key_of(FOREIGN_SEED)]))  # a version that has a key
    assert "unsigned_note" not in dash.library.update_state()
    assert f"{z.name}.sig is missing" in refused(z, app, paths).detail


def test_the_check_comes_before_an_interrupted_rollback_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, app = install_home(tmp_path, monkeypatch), make_app(tmp_path)
    record = rollback_intent.path_for(app)  # an update finishes, undoes or removes such a record first
    record.write_text("{ not a record", encoding="utf-8")
    torn = package(tmp_path / "torn")
    torn.with_name(torn.name + ".sha256").write_text("0" * 64 + "  x\n", encoding="ascii")
    for z, code in ((torn, "NS-E701"), (package(tmp_path / "dl"), "NS-E707")):
        refused(z, app, paths, code)
        assert record.read_text(encoding="utf-8") == "{ not a record", code  # a refused package changed nothing


@pytest.mark.parametrize("source", [SOURCE, None])  # a version that checks the signature, and one with no key yet
def test_a_package_replaced_after_the_check_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str | None
) -> None:
    paths, app = install_home(tmp_path, monkeypatch, source=source), make_app(tmp_path)
    z = package(tmp_path / "dl")
    sign(z)
    before = on_disk(app, paths)

    class Swaps(Env):  # the step made wide: while Nightshift is being stopped, other bytes take the name
        def stop(self) -> None:
            super().stop()
            package(z.parent, payload="FORGED")  # (with a .sha256 of their own; the .sig stays)

    err, e, _said = attempt(z, app, paths, Swaps())
    assert err is not None and err.code == "NS-E702", "the bytes that were not checked were installed"
    assert f"{z.name} was changed after Nightshift checked it" in err.detail and "run the update again" in err.detail
    assert "0.1.0" in err.detail.split("run the update again.")[1]  # it ends with what is in place
    assert e.calls == ["stop"] and on_disk(app, paths) == before  # stopped, and nothing of it installed


# ------------------------------------------------------------------------------------------- the routes
class Stays:
    """The worker's way to start Nightshift again, when Nightshift was running and was never stopped."""

    started = False

    def __init__(self) -> None:
        self.restarts = 0

    def observe(self) -> None:
        return None

    def restart(self) -> None:
        self.restarts += 1  # (the real one starts nothing while the engine answers)

    def running(self) -> bool:
        return True


def stand_ins(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The worker's side effects outside the program folder; returns the stops it asked for."""
    stopped: list[str] = []
    monkeypatch.setattr(cli, "_stop_engine", lambda paths: stopped.append("stop"))
    monkeypatch.setattr(cli, "_reregister", lambda paths, app: [])
    monkeypatch.setattr(update, "verify_quick", lambda app, python=None: True)
    return stopped


def test_a_refusal_in_the_worker_leaves_nightshift_running_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    paths, app = install_home(tmp_path, monkeypatch, port=closed_port), make_app(tmp_path)
    point_home_at(paths, app)
    stopped = stand_ins(monkeypatch)
    z = package(tmp_path / "dl")  # no .sig
    before, relaunch, log = on_disk(app, paths), Stays(), paths.logs / "update-test.log"
    code = update_job.run_worker(
        paths, lambda: cli._apply_update(paths, z, False, False), log=log, relaunch=relaunch, old_version="0.1.0"
    )
    st = update_job.read_status(paths)
    assert code == 1 and st["state"] == "failed" and st["message"].startswith("NS-E707: "), st
    assert f"{z.name}.sig is missing" in st["message"] and st["message"].endswith("Nightshift is running.")
    assert stopped == [] and relaunch.restarts == 1 and on_disk(app, paths) == before
    assert "NS-E707" in log.read_text(encoding="utf-8")


def test_every_route_checks_the_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    from fastapi.testclient import TestClient

    from ns_dashboard.server import create_app
    from ns_dashboard.updater import RealUpdateAdapter
    from proofs._fakes import github

    paths, app = install_home(tmp_path, monkeypatch, port=closed_port), make_app(tmp_path)
    point_home_at(paths, app)
    hand, home = Hand(monkeypatch), {"NIGHTSHIFT_HOME": str(paths.home)}
    stand_ins(monkeypatch)
    good = package(tmp_path / "good")
    forged = package(tmp_path / "forged", payload="FORGED")  # other bytes, their own .sha256, the good .sig
    shutil.copy(sign(good), sig_of(forged))

    def ns(*args: str, **env: str) -> Any:
        return runner.invoke(cli.app, ["update", *args], env={**home, **env})

    # the terminal and the Update shortcut's command: F1, before a worker exists
    for extra in ([], ["--detach", "--watched"]):
        r = ns(str(forged), *extra)
        assert r.exit_code == 1 and "NS-E707" in r.output and "does not match" in r.output, r.output
        assert hand.workers == []
        r = ns(str(good), *extra)
        assert r.exit_code == 0 and len(hand.workers) == 1, r.output
        hand.workers.clear()
    # the worker checks the file again (W4), before the stop
    r = ns(str(forged), "--worker")
    assert r.exit_code == 1 and "NS-E707: " in update_job.read_status(paths)["message"] and version_in(app) == "0.1.0"

    # --github, from the fake GitHub of the proofs over a real socket on this PC
    folder = tmp_path / "served"
    served = package(folder)  # the zip and its .sha256: the fake signs a package once, with the TEST key
    srv = github.serve(folder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        latest = f"http://127.0.0.1:{srv.server_address[1]}/repos/AbdulrahmanAmer/nightshift/releases/latest"
        monkeypatch.setenv(VAR, latest)
        assert getattr(releases.fetch_latest(), "sig_url", None), "the release lists no .sig"
        kept = {p.name: p.read_bytes() for p in folder.iterdir() if p.name.startswith(served.name)}
        package(folder, payload="FORGED")  # a forged release: the zip and its .sha256 changed together
        r = ns("--github", "--worker")
        assert r.exit_code == 1 and "does not match" in r.output, r.output
        sig_of(served).unlink()  # a release without its .sig (the fake never signs a name twice)
        r = ns("--github", "--worker")
        assert r.exit_code == 1 and f"{served.name}.sig is missing" in r.output and version_in(app) == "0.1.0"
        for name, data in kept.items():
            (folder / name).write_bytes(data)
        r = ns("--github", "--worker")
        assert r.exit_code == 0 and "Now on 0.1.16 (was 0.1.0)." in r.output, r.output
        assert "signed with the TEST key" in r.output  # the update log says who signed it
        fetched = paths.data / "updates" / served.name
        assert sig_of(fetched).read_bytes() == kept[sig_of(served).name]  # written beside the zip
        plain = github.serve(tmp_path / "plain", sign=False)  # `--unsigned`: a release from before the key
        package(plain.folder)
        plain.sign_new()
        plain.server_close()
        assert sorted(p.suffix for p in plain.folder.iterdir()) == [".sha256", ".zip"]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)

    # Settings > Updates > From a file: the three files, through the route and the real `ns update <zip>`
    def as_the_dashboard(cmd: list[str], timeout_s: int) -> tuple[int, str, str]:
        done = ns(*cmd[cmd.index("update") + 1 :], **{UNDER_ENGINE_ENV: "1"})
        return done.exit_code, done.stdout, done.stderr

    dash = dashboard(paths)
    dash.state.adapters.update = RealUpdateAdapter(paths, as_the_dashboard)
    site = create_app(state=dash.state, start_worker=False, static_dir=tmp_path / "absent")
    client = TestClient(site, base_url="http://127.0.0.1:8100")
    client.headers["origin"] = "http://127.0.0.1:8100"  # the dashboard's own page (FN-1)

    def post(z: Path, sig: bool = True) -> httpx.Response:
        side = z.with_name(z.name + ".sha256")
        files = {"file": (z.name, z.read_bytes(), "application/zip"), "checksum": (side.name, side.read_bytes())}
        if sig:
            files["signature"] = (sig_of(z).name, sig_of(z).read_bytes())
        return client.post("/api/update/file", files=files)

    saved = paths.data / "updates"
    shutil.rmtree(saved)
    two = post(good, sig=False)
    assert two.status_code == 400 and two.json()["error"]["code"] == "no_signature", two.text
    assert f"Also choose {good.name}.sig" in two.json()["error"]["message"] and not saved.exists()
    bad = post(forged)
    assert bad.status_code == 409 and bad.json()["error"]["message"].startswith("NS-E707: "), bad.text
    assert "does not match" in bad.json()["error"]["message"] and hand.workers == []
    done = post(good)
    assert done.status_code == 200 and done.json()["state"] == "started" and len(hand.workers) == 1, done.text
    assert sig_of(saved / good.name).read_bytes() == sig_of(good).read_bytes()


def test_the_download_fetches_the_sig_and_drops_a_stale_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_home(tmp_path, monkeypatch)
    name, dest = "Nightshift-Update-99.1.0.zip", tmp_path / "dest"
    sig_url = f"{ORIGIN}/download/{name}.sig"

    class Signed(Net):
        status = 200

        def handle(self, req: httpx.Request) -> httpx.Response:
            if str(req.url) != sig_url:
                return super().handle(req)
            self.seen.append(sig_url)
            return httpx.Response(self.status, content=b"alg: ed25519\nthe release's own text\n")

    net = Signed()
    net.fake_json["assets"].append({"name": f"{name}.sig", "browser_download_url": sig_url})
    rel = releases.fetch_latest(net.client())
    assert getattr(rel, "sig_url", None) == sig_url
    z = releases.download(rel, dest, net.client())
    assert sig_of(z).read_bytes() == b"alg: ed25519\nthe release's own text\n"  # as served: the check judges it
    assert net.seen[1:] == [f"{ORIGIN}/download/{name}.sha256", sig_url, f"{ORIGIN}/download/{name}"]
    net.status = 503  # listed, and it cannot be fetched: not "missing", and no zip is asked for
    asked = len(net.seen)
    with pytest.raises(NsError) as ei:
        releases.download(rel, dest, net.client())
    assert ei.value.code == "NS-E118" and not any(u.endswith(".zip") for u in net.seen[asked:])
    elsewhere = releases.parse_release(net.fake_json | {"assets": net.fake_json["assets"][:2]})
    assert elsewhere.sig_url is None  # a release that lists none: the .sig of the earlier download goes
    releases.download(elsewhere, dest, net.client())
    assert z.is_file() and not sig_of(z).exists()
    net.fake_json["assets"][2]["browser_download_url"] = f"https://example.com/{name}.sig"
    assert releases.parse_release(net.fake_json).sig_url is None  # a .sig on another host is not the release's


def test_settings_says_what_the_running_version_checks_and_shows_the_refusal_itself() -> None:
    from ns_dashboard.updater import classify_update, parse_update_output

    table = json.loads((ROOT / "contracts" / "errors.json").read_text(encoding="utf-8"))
    assert "NS-E707" in table, "contracts/errors.json has no NS-E707"
    assert table["NS-E707"]["message"] == (
        "The update package is not signed by a Nightshift release key this version trusts, so it was not "
        "installed."
    )
    # what `ns update <zip>` prints when its check refuses: the coded line, then the code's generic hint
    err = NsError("NS-E707", "Nightshift-Update-0.1.16.zip.sig is missing, so Nightshift cannot tell who made it.")
    shown = classify_update(parse_update_output(1, "", f"{err}\nHint: {err.definition.hint}\n"))
    assert shown["state"] == "error" and shown["message"] == str(err)  # not the hint line alone
    page = (ROOT / "ns_dashboard" / "ui" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    assert 'accept=".zip,.sha256,.sig"' in page and 'fd.append("signature"' in page and "unsigned_note" in page


# --------------------------------------------------------------------- older versions, rotation, the test key
def test_the_update_from_0_1_14_and_the_rollback_to_it_still_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths, app = install_home(tmp_path, monkeypatch), make_app(tmp_path, "0.1.14")
    z = package(tmp_path / "dl")
    sign(z)
    # 0.1.14's own updater (its code cannot change): the zip against its .sha256, the .sig beside it ignored
    assert frozen.verify_package(z) == ("0.1.16", "")
    assert attempt(z, app, paths)[0] is None and version_in(app) == "0.1.16"
    shutil.rmtree(tmp_path / "dl")  # going back needs no package: there is none to check
    assert update.rollback_app(app, Env().env(), paths=paths).new_version == "0.1.14"
    unsigned = package(tmp_path / "dl", "0.1.17")
    assert frozen.verify_package(unsigned) == ("0.1.17", "")  # from 0.1.14 again: as before
    assert update.rollback_app(app, Env().env(), paths=paths).new_version == "0.1.16"
    # the first version that has the check is the one that refuses what 0.1.14 took
    assert f"{unsigned.name}.sig is missing" in refused(unsigned, app, paths).detail


def test_a_key_listed_by_the_new_version_is_trusted_only_from_that_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sigcheck is not None and release_keys is not None, "the verifier or the key list is missing"
    paths = install_home(tmp_path, monkeypatch, runtime=REAL, source=None)
    app = make_app(tmp_path)  # version N
    seed_a, seed_b = (hashlib.sha256(text).digest() for text in (b"release key A", b"release key B"))
    a, b = key_of(seed_a), key_of(seed_b)
    monkeypatch.setattr(release_keys, "TRUSTED", dict([a]))  # N trusts A
    lists = f"TRUSTED = {dict([a, b])!r}\nTEST_KEYS = {{}}\n"
    n1 = package(tmp_path / "dl", "0.1.17", members={"ns_common/release_keys.py": lists})
    sign(n1, seed_a)
    n2 = package(tmp_path / "dl", "0.1.18")
    sign(n2, seed_b)
    err = refused(n2, app, paths)  # N meets N+2: a key it does not know
    assert "does not know" in err.detail and b[0] in err.detail
    assert attempt(n1, app, paths)[0] is None and version_in(app) == "0.1.17"  # N+1, signed by A, brings A and B
    assert refused(n2, app, paths).code == "NS-E707"  # the updater that runs is still N's: a second try, the same
    installed = runpy.run_path(str(app / "ns_common" / "release_keys.py"))["TRUSTED"]
    assert installed == dict([a, b])
    monkeypatch.setattr(release_keys, "TRUSTED", installed)  # the next updater is N+1's program
    assert attempt(n2, app, paths)[0] is None and version_in(app) == "0.1.18"


def test_the_test_key_is_trusted_only_with_a_test_release_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert release_keys is not None and hasattr(releases, "trusted_keys"), "there is no key list yet"
    test_id = key_of(TEST_SEED)[0]
    for n, runtime in enumerate(({"dev_mode": True}, {"tiny_story": True}, {"tts": {"engine": "fake"}})):
        paths = install_home(tmp_path / f"test{n}", monkeypatch, runtime=runtime)
        assert test_id in releases.trusted_keys(paths=paths), runtime  # a dev or engine-test install with a source
        assert releases.trusted_keys({}, paths) == {}  # the same install without the variable
    real = install_home(tmp_path / "real", monkeypatch, runtime=REAL)  # the variable left behind on a real install
    with pytest.raises(NsError) as ei:
        releases.release_source(paths=real)
    assert ei.value.code == "NS-E704" and releases.trusted_keys(paths=real) == {}
    for n, source in enumerate(("http://example.com/releases/latest", "not an address")):
        other = install_home(tmp_path / f"bad{n}", monkeypatch, source=source)  # a test install, a refused value
        assert releases.trusted_keys(paths=other) == {}
    # a file route on the real install: a package signed with the test key
    install_home(tmp_path / "real", monkeypatch, runtime=REAL)
    app = make_app(tmp_path)
    z = package(tmp_path / "dl")
    sign(z)
    monkeypatch.setattr(release_keys, "TRUSTED", dict([key_of(FOREIGN_SEED)]))  # it trusts a real key
    err = refused(z, app, real)
    assert "does not know" in err.detail and test_id in err.detail
    monkeypatch.setattr(release_keys, "TRUSTED", {})  # before the owner's step: SHA256 only, and it says so
    err, _e, said = attempt(z, app, real)
    assert err is None and any(NOTE in line for line in said) and not any("signed with" in line for line in said)
