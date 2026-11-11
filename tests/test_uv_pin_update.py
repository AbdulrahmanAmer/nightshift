"""Item uv-pin-on-update (F-E28): an update and a rollback bring the uv their program pins, before its lock.

WRITTEN, NOT RUN (the owner's build method of 2026-11-11: tests run once, after the block is merged).

uv stands as a small text file ``uv.exe`` whose content is what ``uv --version`` prints ("uv 0.14.0 ...");
``uvpin.run_version`` is replaced by a reader of that file, so no process is started. The uv zip is a zip of
that file with its SHA256 in the target's ``manifest.json``. Nothing reaches the network: ``uvpin.download``
is replaced by ``Net``, which serves the bytes a test gives it and refuses every other address.
Entry: ``update.update_app`` and ``update.rollback_app`` on a tmp install built as ``tests/test_ns_update.py``
builds it. Not covered here: a real uv, a real kill of the worker by pid (a ``KeyboardInterrupt`` raised at
that point stands for it), the export of a locked program (the venv test uses a program without a lock).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from installer.stage2.web import uvpin, venvrepair
from ns_common import rollback_intent
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import doctor, update
from tests.test_ns_update import Env, make_app, make_zip

LEAF = "uv-x86_64-pc-windows-msvc.zip"
REAL_DOWNLOAD = uvpin.download


def url(version: str) -> str:
    return f"https://github.com/astral-sh/uv/releases/download/{version}/{LEAF}"


def put_uv(folder: Path, version: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "uv.exe").write_text(f"uv {version} (stand-in)\n", encoding="utf-8")


def version_at(folder: Path) -> str | None:
    return uvpin.report(folder / "uv.exe")[1]


def uv_zip(dest: Path, version: str, text: str | None = None) -> str:
    """A uv zip of the stand-in at ``dest``; its SHA256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w") as z:
        z.writestr("uv-x86_64-pc-windows-msvc/uv.exe", text or f"uv {version} (stand-in)\n")
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def manifest(version: str, sha: str) -> str:
    return json.dumps({"bins": {"uv": {"version": version, "url": url(version), "sha256": sha}}})


class Net:
    """Stands for GitHub: serves what a test put in ``serve``; every other address is "no network"."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.serve: dict[str, bytes] = {}

    def __call__(self, address: str, dest: Path) -> None:
        self.calls.append(address)
        if address not in self.serve:
            raise OSError("no network in this test")
        dest.write_bytes(self.serve[address])


@pytest.fixture(autouse=True)
def net(monkeypatch: pytest.MonkeyPatch) -> Net:
    def run_version(exe: Path) -> tuple[int, str]:
        text = exe.read_text(encoding="utf-8")
        return (0, text) if text.startswith("uv ") else (1, "")

    fake = Net()
    monkeypatch.setattr(uvpin, "run_version", run_version)
    monkeypatch.setattr(uvpin, "download", fake)
    monkeypatch.setattr(update, "RENAME_TRIES", 2)
    monkeypatch.setattr(update, "RENAME_PAUSE_S", 0.0)
    return fake


@dataclasses.dataclass
class Install:
    tmp: Path
    app: Path
    paths: Paths

    @property
    def bin(self) -> Path:
        return self.paths.home / "bin"

    def uv(self, suffix: str = "") -> str | None:
        return version_at(self.bin / f"uv{suffix}")

    def version(self) -> str:
        return (self.app / "VERSION").read_text(encoding="utf-8").strip()

    def venv(self, name: str = "studio") -> None:
        py = self.paths.venvs / name / "Scripts" / "python.exe"
        py.parent.mkdir(parents=True)
        py.write_text("", encoding="utf-8")


def install(tmp_path: Path, have: str = "0.13.6", old_sha: str = "0" * 64) -> Install:
    """Program 0.1.0, which pins uv ``have``, and that uv in ``bin\\uv``."""
    app = make_app(tmp_path)
    paths = Paths(tmp_path / "ns")
    put_uv(paths.home / "bin" / "uv", have)
    (app / "manifest.json").write_text(manifest(have, old_sha), encoding="utf-8")
    return Install(tmp_path, app, paths)


def package(tmp_path: Path, version: str = "0.14.0", *, files: bool = True) -> Path:
    """The update zip of 0.2.0, whose manifest pins uv ``version``; with ``files`` the uv zip beside it."""
    staged = tmp_path / "stage" / LEAF
    sha = uv_zip(staged, version)
    if files:
        (tmp_path / "files").mkdir(exist_ok=True)
        shutil.copyfile(staged, tmp_path / "files" / LEAF)
    return make_zip(tmp_path, extra={"manifest.json": manifest(version, sha)})


def test_an_update_whose_pin_moved_brings_the_new_uv_before_the_lock_is_read(tmp_path: Path) -> None:
    inst, z = install(tmp_path), package(tmp_path)
    inst.venv()
    seen: list[tuple[str | None, list[str]]] = []

    def run(cmd: list[str]) -> tuple[int, str]:
        seen.append((inst.uv(), cmd))
        return 0, ""

    def sync(app: Path) -> None:
        update.resync_venvs(app, inst.paths, run=run)

    update.update_app(z, inst.app, dataclasses.replace(Env().env(), sync=sync), paths=inst.paths)
    exe = str(inst.bin / "uv" / "uv.exe")
    assert seen, "the sync ran no uv command"
    assert all(at == "0.14.0" and cmd[0] == exe for at, cmd in seen)  # base: every one ran with 0.13.6
    assert (inst.uv(), inst.uv(".prev")) == ("0.14.0", "0.13.6")
    assert not (inst.bin / "uv.next").exists()
    assert (inst.paths.home / "cache" / "uv-0.14.0.zip").is_file()  # kept for a rollback or a second try
    assert inst.version() == "0.2.0"


def test_the_same_pin_changes_nothing(tmp_path: Path, net: Net) -> None:
    inst = install(tmp_path)
    put_uv(inst.bin / "uv.prev", "0.12.0")
    exe = inst.bin / "uv" / "uv.exe"
    before = exe.stat().st_mtime_ns
    update.update_app(package(tmp_path, "0.13.6", files=False), inst.app, Env().env(), paths=inst.paths)
    assert (inst.uv(), inst.uv(".prev")) == ("0.13.6", "0.12.0")
    assert exe.stat().st_mtime_ns == before and net.calls == []
    assert not (inst.paths.home / "cache" / "uv-0.13.6.zip").exists()
    assert inst.version() == "0.2.0"


def test_a_program_that_pins_no_uv_keeps_the_installs_uv(tmp_path: Path, net: Net) -> None:
    inst = install(tmp_path)
    update.update_app(make_zip(tmp_path), inst.app, Env().env(), paths=inst.paths)  # no manifest.json in it
    assert inst.uv() == "0.13.6" and not (inst.bin / "uv.prev").exists() and net.calls == []
    update.rollback_app(inst.app, Env().env(), paths=inst.paths)
    assert inst.uv() == "0.13.6" and inst.version() == "0.1.0"


def test_the_uv_comes_from_files_then_cache_then_github(tmp_path: Path, net: Net) -> None:
    said: list[str] = []
    # 1. a files folder whose zip has other bytes is skipped with one line; the cache has the pinned one
    home = tmp_path / "ns1"
    put_uv(home / "bin" / "uv", "0.13.6")
    sha = uv_zip(home / "cache" / "uv-0.14.0.zip", "0.14.0")
    uv_zip(tmp_path / "files" / LEAF, "0.14.0", text="uv 0.14.0 (another build)\n")
    pin = {"version": "0.14.0", "url": url("0.14.0"), "sha256": sha}
    plan = uvpin.prepare(pin, home, files_root=tmp_path / "files", say=said.append)
    assert plan.how == "next" and version_at(home / "bin" / "uv.next") == "0.14.0"
    assert said == [f"files\\{LEAF} does not match its pin; trying the next source"]
    assert net.calls == []
    # 2. a files folder that has it wins over everything, and the cache keeps a copy
    home = tmp_path / "ns2"
    put_uv(home / "bin" / "uv", "0.13.6")
    sha = uv_zip(tmp_path / "good" / "files" / LEAF, "0.14.0")
    pin = {"version": "0.14.0", "url": url("0.14.0"), "sha256": sha}
    plan = uvpin.prepare(pin, home, files_root=tmp_path / "good" / "files", say=said.append)
    assert plan.how == "next" and net.calls == []
    assert hashlib.sha256((home / "cache" / "uv-0.14.0.zip").read_bytes()).hexdigest() == sha
    # 3. no files folder, a cache with other bytes (one line), then the pin's address
    home = tmp_path / "ns3"
    put_uv(home / "bin" / "uv", "0.13.6")
    uv_zip(home / "cache" / "uv-0.14.0.zip", "0.14.0", text="uv 0.14.0 (tampered)\n")
    net.serve[url("0.14.0")] = (tmp_path / "good" / "files" / LEAF).read_bytes()
    said.clear()
    plan = uvpin.prepare(pin, home, say=said.append)
    assert plan.how == "next" and net.calls == [url("0.14.0")]
    assert said == ["cache\\uv-0.14.0.zip does not match its pin; trying the next source"]
    assert hashlib.sha256((home / "cache" / "uv-0.14.0.zip").read_bytes()).hexdigest() == sha
    assert not (home / "cache" / "uv-0.14.0.zip.part").exists()
    # 4. the address answers with other bytes: refused, nothing kept
    home = tmp_path / "ns4"
    put_uv(home / "bin" / "uv", "0.13.6")
    net.serve[url("0.14.0")] = b"not the pinned bytes"
    with pytest.raises(uvpin.NotHad) as ei:
        uvpin.prepare(pin, home, say=said.append)
    assert "does not match its SHA256" in str(ei.value) and LEAF in str(ei.value)
    assert not list((home / "cache").glob("uv-0.14.0.zip*")) and not (home / "bin" / "uv.next").exists()


def test_the_download_refuses_an_address_that_is_not_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NS_RELEASE_SOURCE", raising=False)
    with pytest.raises(OSError, match="not an address Nightshift downloads from"):
        REAL_DOWNLOAD("https://example.com/uv.zip", tmp_path / "x.zip")  # refused before any request
    assert not (tmp_path / "x.zip").exists()


def test_no_source_refuses_before_any_change(tmp_path: Path, net: Net) -> None:
    inst, z = install(tmp_path), package(tmp_path, files=False)
    before = update.app_fingerprint(inst.app)
    e = Env()
    with pytest.raises(NsError) as ei:
        update.update_app(z, inst.app, e.env(), paths=inst.paths)
    detail = ei.value.detail
    assert ei.value.code == "NS-E708"
    assert "This update needs uv 0.14.0." in detail and LEAF in detail
    assert "GitHub could not be reached" in detail and "Nothing was changed" in detail
    assert e.calls == []  # Nightshift was not stopped: what ran before the refusal is still running
    assert update.app_fingerprint(inst.app) == before and inst.version() == "0.1.0"
    assert not (tmp_path / "NightShift.prev").exists() and not (tmp_path / "NightShift.new").exists()
    assert inst.uv() == "0.13.6" and not list(inst.bin.glob("uv.*"))  # no bin\uv.prev, no bin\uv.next
    assert not venvrepair.record_path(inst.paths.home).exists()
    assert net.calls == [url("0.14.0")]


def test_a_failed_update_after_the_uv_swap_puts_the_old_uv_back(tmp_path: Path) -> None:
    inst, z = install(tmp_path), package(tmp_path)
    seen: list[str | None] = []

    def sync(app: Path) -> None:
        seen.append(inst.uv())
        if len(seen) == 1:
            raise NsError("NS-E702", "uv exploded")

    with pytest.raises(NsError) as ei:
        update.update_app(z, inst.app, dataclasses.replace(Env().env(), sync=sync), paths=inst.paths)
    assert seen == ["0.14.0", "0.13.6"]  # the new program's sync, then the restore's re-sync with the old uv
    assert ei.value.code == "NS-E702" and "uv is back at 0.13.6." in ei.value.detail
    assert inst.uv() == "0.13.6" and not list(inst.bin.glob("uv.*"))  # no bin\uv.prev, no bin\uv.next
    assert inst.version() == "0.1.0"


def test_a_refusal_between_the_uv_swap_and_the_program_swap_puts_the_old_uv_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst, z = install(tmp_path), package(tmp_path)
    at: list[str | None] = []

    def no_record(*a: object, **kw: object) -> bool:
        at.append(inst.uv())
        raise OSError("disk full")

    monkeypatch.setattr(update, "write_record", no_record)
    with pytest.raises(NsError) as ei:
        update.update_app(z, inst.app, Env().env(), paths=inst.paths)
    assert at == ["0.14.0"]  # the record is written after the uv swap
    assert ei.value.code == "NS-E702" and "could not record the update" in ei.value.detail
    assert inst.uv() == "0.13.6" and not (inst.bin / "uv.next").exists() and inst.version() == "0.1.0"


def test_a_rollback_goes_back_to_the_older_programs_uv(tmp_path: Path, net: Net) -> None:
    inst = install(tmp_path)
    update.update_app(package(tmp_path), inst.app, Env().env(), paths=inst.paths)
    seen: list[str | None] = []
    env = dataclasses.replace(Env().env(), sync=lambda app: seen.append(inst.uv()))
    res = update.rollback_app(inst.app, env, paths=inst.paths)
    assert res.new_version == "0.1.0" and seen == ["0.13.6"]  # swapped back before the rollback's sync
    assert (inst.uv(), inst.uv(".prev")) == ("0.13.6", "0.14.0")
    assert "uv stays" not in res.settings and net.calls == []
    update.rollback_app(inst.app, Env().env(), paths=inst.paths)  # and forward again
    assert (inst.uv(), inst.uv(".prev")) == ("0.14.0", "0.13.6") and inst.version() == "0.2.0"


def test_a_rollback_without_the_older_uv_fetches_it_or_says_that_uv_stays(tmp_path: Path, net: Net) -> None:
    old_sha = uv_zip(tmp_path / "served" / LEAF, "0.13.6")
    inst = install(tmp_path, old_sha=old_sha)
    update.update_app(package(tmp_path), inst.app, Env().env(), paths=inst.paths)
    shutil.rmtree(inst.bin / "uv.prev")
    res = update.rollback_app(inst.app, Env().env(), paths=inst.paths)  # offline: done, and said
    assert res.new_version == "0.1.0" and inst.uv() == "0.14.0"
    assert "uv stays at 0.14.0 (" in res.settings and "pins 0.13.6 and it could not be had" in res.settings
    assert net.calls == [url("0.13.6")]
    update.rollback_app(inst.app, Env().env(), paths=inst.paths)  # forward: 0.2.0 pins the uv in place
    assert inst.uv() == "0.14.0" and inst.version() == "0.2.0"
    net.serve[url("0.13.6")] = (tmp_path / "served" / LEAF).read_bytes()
    res = update.rollback_app(inst.app, Env().env(), paths=inst.paths)  # fetched like an update's
    assert (inst.uv(), inst.uv(".prev")) == ("0.13.6", "0.14.0") and "uv stays" not in res.settings


def rolled_forward(tmp_path: Path, *, cached: bool) -> Install:
    """0.1.0 (pins 0.13.6) updated to 0.2.0 (pins 0.14.0), then bin\\uv.prev removed, so a rollback has to get
    0.13.6 again; with `cached` its checked zip is in the install's cache (no network needed)."""
    old_sha = uv_zip(tmp_path / "served" / LEAF, "0.13.6")
    inst = install(tmp_path, old_sha=old_sha)
    update.update_app(package(tmp_path), inst.app, Env().env(), paths=inst.paths)
    shutil.rmtree(inst.bin / "uv.prev")
    if cached:
        shutil.copyfile(tmp_path / "served" / LEAF, inst.paths.home / "cache" / "uv-0.13.6.zip")
    return inst


def test_a_rollback_gets_its_uv_before_the_record_and_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2 (written, NOT RUN): U1 to U4 of a rollback run while nothing is changed yet: no rollback record on
    disk and the settings not prepared; the swap itself (U5) comes after both."""
    inst = rolled_forward(tmp_path, cached=True)
    real_for, real_swap = update.uv_for, update.uv_swap
    seen: list[tuple[str, bool]] = []
    record = rollback_intent.path_for(inst.app)

    def spy_for(*a: object, **kw: object) -> uvpin.Plan:
        seen.append(("uv_for", record.exists()))
        return real_for(*a, **kw)  # type: ignore[arg-type]

    def spy_swap(*a: object, **kw: object) -> None:
        seen.append(("uv_swap", record.exists()))
        real_swap(*a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(update, "uv_for", spy_for)
    monkeypatch.setattr(update, "uv_swap", spy_swap)
    res = update.rollback_app(inst.app, Env().env(), paths=inst.paths)
    assert seen == [("uv_for", False), ("uv_swap", True)]
    assert res.new_version == "0.1.0" and (inst.uv(), inst.uv(".prev")) == ("0.13.6", "0.14.0")


@pytest.mark.parametrize("where", ["the rollback record", "the venv record"])
def test_a_refusal_after_the_rollbacks_uv_was_got_puts_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """B2 (written, NOT RUN): a way out between U4 and the program swap, before the uv swap (the rollback
    record cannot be written) or after it (the venv record cannot be written): no bin\\uv.next is left, the
    newer program's uv is in bin\\uv again, and 0.2.0 is still the program."""
    inst = rolled_forward(tmp_path, cached=True)

    def refused(*a: object, **kw: object) -> bool:
        raise OSError("disk full")

    with monkeypatch.context() as m:
        if where == "the rollback record":
            m.setattr(rollback_intent, "write", refused)
        else:
            m.setattr(update, "write_record", refused)
        with pytest.raises(NsError) as ei:
            update.rollback_app(inst.app, Env().env(), paths=inst.paths)
    assert ei.value.code == "NS-E702" and "could not record the rollback" in ei.value.detail
    assert inst.uv() == "0.14.0" and inst.version() == "0.2.0"
    assert not (inst.bin / "uv.next").exists() and not (inst.bin / "uv.prev").exists()
    assert not (inst.bin / "uv.swap").exists()


def test_an_unforeseen_error_while_getting_the_rollbacks_uv_keeps_the_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2 (written, NOT RUN): an error that is neither NotHad nor Held (psutil failing in the holder check of
    bin\\uv) leaves no bin\\uv.next and changes nothing; the rollback then goes on with the uv there is and
    says so, as for a uv that could not be had."""
    inst = rolled_forward(tmp_path, cached=True)
    real = update.holders

    def broken(paths: Paths, venv: str | None = None, *, root: Path | None = None) -> list[update.Holder]:
        if root is not None:
            raise RuntimeError("access denied")
        return real(paths, venv)

    monkeypatch.setattr(update, "holders", broken)
    res = update.rollback_app(inst.app, Env().env(), paths=inst.paths)
    assert res.new_version == "0.1.0" and inst.uv() == "0.14.0"
    said = "uv stays at 0.14.0 (0.1.0 pins 0.13.6, and looking for it failed: RuntimeError: access denied)"
    assert said in res.settings
    assert not (inst.bin / "uv.next").exists() and not rollback_intent.path_for(inst.app).exists()


def test_a_zip_exchanged_after_the_check_never_gets_its_uv_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    """B3 (written, NOT RUN): the pin comes from the bytes `_check_package` hashed. A zip replaced under the
    same name right after the check (the dashboard still takes an upload) pins uv 0.15.0 with a uv zip of
    its own in `files`: that uv is never unpacked, fetched or run; W4b then refuses the changed file."""
    inst, z = install(tmp_path), package(tmp_path)
    assert update._check_package(z, None).uv_pin["version"] == "0.14.0"
    evil_sha = uv_zip(tmp_path / "files" / "uv-0.15.0.zip", "0.15.0", text="uv 0.15.0 (not signed)\n")
    (tmp_path / "evil").mkdir()
    evil = make_zip(tmp_path / "evil", extra={"manifest.json": manifest("0.15.0", evil_sha)})
    real_check, real_run = update._check_package, uvpin.run_version
    ran: list[str] = []

    def exchanged(zip_path: Path, say: object) -> object:
        checked = real_check(zip_path, say)  # type: ignore[arg-type]
        zip_path.write_bytes(evil.read_bytes())     # replaced right after the check, same name
        return checked

    def run(exe: Path) -> tuple[int, str]:
        code, out = real_run(exe)
        ran.append(out)
        return code, out

    monkeypatch.setattr(update, "_check_package", exchanged)
    monkeypatch.setattr(uvpin, "run_version", run)
    with pytest.raises(NsError) as ei:
        update.update_app(z, inst.app, Env().env(), paths=inst.paths)
    assert ei.value.code == "NS-E702" and "was changed after Nightshift checked it" in ei.value.detail
    assert not any("0.15.0" in out for out in ran), ran   # the unsigned pin's uv never ran
    assert any("0.14.0" in out for out in ran)            # the signed pin's uv was the one checked
    assert not (inst.paths.home / "cache" / "uv-0.15.0.zip").exists() and net.calls == []
    assert inst.uv() == "0.13.6" and not list(inst.bin.glob("uv.*")) and inst.version() == "0.1.0"


def test_the_repair_record_names_the_uv_that_matches_the_program(tmp_path: Path) -> None:
    inst, z = install(tmp_path), package(tmp_path)
    inst.venv()
    seen: list[tuple[str, str | None, bool]] = []

    def sync(app: Path) -> None:
        record = venvrepair.read(inst.paths.home)
        assert record is not None, "no repair record was written before the sync"
        replay = all(c["argv"][0] == record["uv"] for c in record["commands"])
        seen.append((record["uv"], version_at(Path(record["uv"]).parent), replay))

    update.update_app(z, inst.app, dataclasses.replace(Env().env(), sync=sync), paths=inst.paths)
    assert seen == [(str(inst.bin / "uv" / "uv.exe"), "0.14.0", True)]


def test_killed_between_the_uv_swap_and_the_program_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst, z = install(tmp_path), package(tmp_path)

    def killed(*a: object, **kw: object) -> bool:
        raise KeyboardInterrupt  # stands for the worker ended by its pid: no handler of the update runs

    with monkeypatch.context() as m:
        m.setattr(update, "write_record", killed)
        with pytest.raises(KeyboardInterrupt):
            update.update_app(z, inst.app, Env().env(), paths=inst.paths)
    assert (inst.uv(), inst.uv(".prev"), inst.version()) == ("0.14.0", "0.13.6", "0.1.0")
    assert doctor.uv_row(inst.paths, inst.app) == (
        "uv",
        True,
        doctor.WARN + "uv 0.14.0, but this program pins 0.13.6. "
        "Run INSTALL.cmd again and choose Repair to put the pinned uv back.",
    )
    update.update_app(z, inst.app, Env().env(), paths=inst.paths)  # the same update again: no damage
    assert (inst.uv(), inst.uv(".prev"), inst.version()) == ("0.14.0", "0.13.6", "0.2.0")
    assert doctor.uv_row(inst.paths, inst.app) == ("uv", True, "uv 0.14.0, the version this program pins")
    update.rollback_app(inst.app, Env().env(), paths=inst.paths)
    assert (inst.uv(), inst.uv(".prev"), inst.version()) == ("0.13.6", "0.14.0", "0.1.0")


def test_a_swap_cut_between_its_two_renames_is_settled_by_the_next_run(tmp_path: Path) -> None:
    # an update's swap: bin\uv already moved to bin\uv.prev, bin\uv.next not yet in place
    inst = install(tmp_path)
    (inst.bin / "uv").rename(inst.bin / "uv.prev")
    put_uv(inst.bin / "uv.next", "0.14.0")
    update.update_app(package(tmp_path), inst.app, Env().env(), paths=inst.paths)
    assert (inst.uv(), inst.uv(".prev")) == ("0.14.0", "0.13.6") and not (inst.bin / "uv.next").exists()
    # an exchange: bin\uv moved aside to bin\uv.swap, bin\uv.prev not yet in its place
    home = tmp_path / "other"
    put_uv(home / "bin" / "uv.swap", "0.14.0")
    put_uv(home / "bin" / "uv.prev", "0.13.6")
    uvpin.settle(home)
    assert version_at(home / "bin" / "uv") == "0.13.6" and version_at(home / "bin" / "uv.prev") == "0.14.0"
    assert not (home / "bin" / "uv.swap").exists()


def three_uvs(tmp_path: Path) -> tuple[Path, uvpin.Plan]:
    """A home in the state just before S1: bin\\uv 0.13.6 (the program in <app>), bin\\uv.prev 0.12.0 (the
    program in <app>.prev), bin\\uv.next 0.14.0 (fetched for the update); the plan `prepare` would make."""
    home = tmp_path / "ns"
    put_uv(home / "bin" / "uv", "0.13.6")
    put_uv(home / "bin" / "uv.prev", "0.12.0")
    put_uv(home / "bin" / "uv.next", "0.14.0")
    plan = uvpin.Plan(home, want="0.14.0", was="0.13.6", how="next", older={"version": "0.12.0"})
    return home, plan


def uvs(home: Path) -> tuple[str | None, str | None, str | None, str | None]:
    """(bin\\uv, bin\\uv.prev, bin\\uv.swap, bin\\uv.next): the version each reports, None when absent."""
    at = [version_at(home / "bin" / f"uv{s}") for s in ("", ".prev", ".swap", ".next")]
    return at[0], at[1], at[2], at[3]


def failing_at(step: int, exc: BaseException) -> uvpin.Rename:
    """A rename that does the first `step - 1` renames and raises `exc` at rename number `step`."""
    done: list[tuple[Path, Path]] = []

    def rename(src: Path, dst: Path) -> None:
        if len(done) + 1 == step:
            raise exc
        done.append((src, dst))
        src.rename(dst)

    return rename


def put_back(plan: uvpin.Plan) -> str:
    """``Plan.undo``, called through a name: tests/test_no_real_machine.py
    (``test_no_test_calls_undo_on_its_monkeypatch``) reads every ``<x>.undo()`` in a test file as a
    monkeypatch's and fails on it."""
    undo = plan.undo
    return undo()


@pytest.mark.parametrize("step", [1, 2, 3])
def test_a_refused_rename_keeps_the_older_programs_uv(tmp_path: Path, step: int) -> None:
    """B1 (written, NOT RUN): S1, S2 or S3 refused. What moved before it is put back and `undo` (which
    `update.uv_swap` calls next) finds nothing left to do: bin\\uv.prev still reports 0.12.0."""
    home, plan = three_uvs(tmp_path)
    with pytest.raises(OSError, match="held"):
        plan.swap(failing_at(step, PermissionError("held by another program")))
    assert put_back(plan) == ""
    assert uvs(home) == ("0.13.6", "0.12.0", None, None)


@pytest.mark.parametrize(
    ("step", "older", "after"),
    [
        (1, {"version": "0.12.0"}, ("0.13.6", "0.12.0", None, None)),   # killed after S1
        (2, {"version": "0.12.0"}, ("0.13.6", "0.12.0", None, None)),   # killed after S2
        (3, {"version": "0.12.0"}, ("0.13.6", "0.12.0", None, None)),   # after S3, program swap not done
        (3, {"version": "0.13.6"}, ("0.14.0", "0.13.6", None, None)),   # after S3 and the program swap
        (3, {}, ("0.14.0", "0.13.6", None, None)),                       # <app>.prev gone or pins none
        (3, None, ("0.14.0", "0.13.6", "0.12.0", None)),                 # not known: nothing is deleted
    ],
)
def test_a_kill_after_each_step_of_the_swap_is_settled_by_the_next_run(
    tmp_path: Path, step: int, older: dict[str, str] | None, after: tuple[str | None, ...]
) -> None:
    """B1 (written, NOT RUN): the worker killed right after S`step` (a KeyboardInterrupt at the next rename,
    or no `finish` after S3), then the next update or rollback's `settle` with the pin of <app>.prev then."""
    home, plan = three_uvs(tmp_path)
    if step < 3:
        with pytest.raises(KeyboardInterrupt):
            plan.swap(failing_at(step + 1, KeyboardInterrupt()))
    else:
        plan.swap()                              # S3 done; the kill comes before `finish`
    assert uvs(home)[2] == "0.12.0"              # the older program's uv is kept aside, never deleted
    uvpin.settle(home, older)
    assert uvs(home) == after


def test_the_uv_kept_aside_goes_back_or_goes_by_the_program_in_prev(tmp_path: Path) -> None:
    """B1 (written, NOT RUN): `undo` after S3 (U6: a refusal before the program swap) puts both back; `finish`
    after the program swap removes the kept uv, or makes it bin\\uv.prev when <app>.prev pins it."""
    home, plan = three_uvs(tmp_path)
    plan.swap()
    assert uvs(home) == ("0.14.0", "0.13.6", "0.12.0", None)
    assert put_back(plan) == " uv is back at 0.13.6."
    assert uvs(home) == ("0.13.6", "0.12.0", None, None)
    home, plan = three_uvs(tmp_path / "2")
    plan.swap()
    plan.finish({"version": "0.13.6"})          # the update's program swap: <app>.prev is 0.13.6's program
    assert uvs(home) == ("0.14.0", "0.13.6", None, None)
    home, plan = three_uvs(tmp_path / "3")
    plan.swap()
    plan.finish({"version": "0.12.0"})          # no program swap followed (a finished rollback's own uv)
    assert uvs(home) == ("0.14.0", "0.12.0", None, None)


def older_program(inst: Install, uv: str) -> None:
    """<app>.prev: program 0.0.9, which pins uv `uv`, and that uv in bin\\uv.prev."""
    prev = inst.app.with_name(inst.app.name + ".prev")
    shutil.copytree(inst.app, prev)
    (prev / "VERSION").write_text("0.0.9\n", encoding="utf-8")
    (prev / "manifest.json").write_text(manifest(uv, "0" * 64), encoding="utf-8")
    put_uv(inst.bin / "uv.prev", uv)


def test_a_refusal_between_the_two_swaps_keeps_the_older_programs_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1 (written, NOT RUN): U6 with a bin\\uv.prev there. The refusal says nothing changed, and nothing did:
    bin\\uv.prev still reports the uv <app>.prev pins. The same update done afterwards replaces both."""
    inst, z = install(tmp_path), package(tmp_path)
    older_program(inst, "0.12.0")

    def no_record(*a: object, **kw: object) -> bool:
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(update, "write_record", no_record)
        with pytest.raises(NsError) as ei:
            update.update_app(z, inst.app, Env().env(), paths=inst.paths)
    assert ei.value.code == "NS-E702" and "could not record the update" in ei.value.detail
    assert (inst.uv(), inst.uv(".prev"), inst.version()) == ("0.13.6", "0.12.0", "0.1.0")
    assert not (inst.bin / "uv.swap").exists() and not (inst.bin / "uv.next").exists()
    update.update_app(z, inst.app, Env().env(), paths=inst.paths)
    assert (inst.uv(), inst.uv(".prev"), inst.version()) == ("0.14.0", "0.13.6", "0.2.0")
    assert not (inst.bin / "uv.swap").exists()   # `finish`: 0.12.0's program left <app>.prev with the swap


@pytest.mark.parametrize(
    ("text", "said"),
    [("broken\n", "does not run on this PC"), ("uv 0.13.9 (stand-in)\n", "reports 0.13.9, not 0.14.0")],
)
def test_a_uv_that_does_not_run_is_refused(tmp_path: Path, text: str, said: str) -> None:
    inst = install(tmp_path)
    sha = uv_zip(tmp_path / "files" / LEAF, "0.14.0", text=text)
    z = make_zip(tmp_path, extra={"manifest.json": manifest("0.14.0", sha)})
    e = Env()
    with pytest.raises(NsError) as ei:
        update.update_app(z, inst.app, e.env(), paths=inst.paths)
    assert ei.value.code == "NS-E708" and said in ei.value.detail and "Nothing was changed" in ei.value.detail
    assert not (inst.bin / "uv.next").exists() and not (inst.bin / "uv.prev").exists()
    assert inst.uv() == "0.13.6" and inst.version() == "0.1.0" and e.calls == []


def test_no_uv_next_is_left_when_the_update_is_refused_for_running_programs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NOT RUN. NS-E706 comes after the uv was unpacked and checked: `bin\\uv.next` goes with the refusal."""
    inst, z = install(tmp_path), package(tmp_path)
    at: list[bool] = []

    def refuse(*a: object, **kw: object) -> None:
        at.append((inst.bin / "uv.next" / "uv.exe").is_file())
        raise NsError("NS-E706", "These programs still run from Nightshift's Python environments")

    monkeypatch.setattr(update, "wait_no_holders", refuse)
    with pytest.raises(NsError) as ei:
        update.update_app(z, inst.app, Env().env(), paths=inst.paths)
    assert ei.value.code == "NS-E706" and at == [True]  # it was there when the refusal came
    assert inst.uv() == "0.13.6" and not list(inst.bin.glob("uv.*")) and inst.version() == "0.1.0"


def test_a_running_uv_refuses_the_update_before_nightshift_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    """NOT RUN. D-39a's holder check covers `bin\\uv`: NS-E706 naming the program, before the stop."""
    inst, z = install(tmp_path), package(tmp_path)
    asked: list[Path | None] = []

    def running(paths: Paths, venv: str | None = None, *, root: Path | None = None) -> list[update.Holder]:
        asked.append(root)
        return [update.Holder(4242, "uv.exe", "12:00:00")] if root == inst.bin / "uv" else []

    monkeypatch.setattr(update, "holders", running)
    e = Env()
    with pytest.raises(NsError) as ei:
        update.update_app(z, inst.app, e.env(), paths=inst.paths)
    detail = ei.value.detail
    assert ei.value.code == "NS-E706" and "uv.exe (process 4242, started 12:00:00)" in detail
    assert "Nothing was changed" in detail and "Nightshift was not stopped." in detail
    assert "run the update again" in detail and "restart the PC" in detail  # what the owner does next
    assert e.calls == [] and asked == [inst.bin / "uv"]
    assert inst.uv() == "0.13.6" and not list(inst.bin.glob("uv.*")) and inst.version() == "0.1.0"


def test_a_uv_that_cannot_be_opened_is_could_not_look_not_damaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    """NOT RUN. A uv another program holds open does not answer --version: it is not replaced or fetched."""
    inst, z = install(tmp_path), package(tmp_path)
    exe = inst.bin / "uv" / "uv.exe"
    exe.write_text("no answer\n", encoding="utf-8")  # (the stand-in's --version fails for this content)
    monkeypatch.setattr(uvpin, "held", lambda path: "PermissionError: used by another process")
    e = Env()
    with pytest.raises(NsError) as ei:
        update.update_app(z, inst.app, e.env(), paths=inst.paths)
    detail = ei.value.detail
    assert ei.value.code == "NS-E706" and "could not look at its uv" in detail and str(exe) in detail
    assert "Nothing was changed" in detail and "Nightshift was not stopped." in detail
    assert e.calls == [] and net.calls == [] and not list(inst.bin.glob("uv.*"))
    assert not (inst.paths.home / "cache" / "uv-0.14.0.zip").exists() and inst.version() == "0.1.0"
    # the same uv, not held and not running, is a damaged one: it is replaced
    monkeypatch.setattr(uvpin, "held", lambda path: None)
    update.update_app(z, inst.app, Env().env(), paths=inst.paths)
    assert inst.uv() == "0.14.0" and inst.version() == "0.2.0"


def test_the_refusal_names_the_files_folder_of_the_dashboard_route(tmp_path: Path, net: Net) -> None:
    """NOT RUN. "From a file" and `--github` keep the zip in `<home>\\data\\updates`: NS-E708 names
    `<home>\\data\\updates\\files` by its path, and the uv zip put there is taken on the next try."""
    inst = install(tmp_path)
    updates = inst.paths.data / "updates"
    updates.mkdir(parents=True)
    sha = uv_zip(tmp_path / "stage" / LEAF, "0.14.0")
    z = make_zip(updates, extra={"manifest.json": manifest("0.14.0", sha)})
    with pytest.raises(NsError) as ei:
        update.update_app(z, inst.app, Env().env(), paths=inst.paths)
    assert ei.value.code == "NS-E708"
    assert str(updates / "files") in ei.value.detail and str(inst.paths.home / "cache") in ei.value.detail
    (updates / "files").mkdir()
    shutil.copyfile(tmp_path / "stage" / LEAF, updates / "files" / LEAF)
    update.update_app(z, inst.app, Env().env(), paths=inst.paths)
    assert (inst.uv(), inst.uv(".prev"), inst.version()) == ("0.14.0", "0.13.6", "0.2.0")
    assert net.calls == [url("0.14.0")]  # only the first, refused try asked GitHub


def test_a_left_over_uv_next_is_cleared_by_the_next_update_or_rollback(tmp_path: Path) -> None:
    """NOT RUN. After a kill (no handler ran): the next update clears it even when its program pins no uv,
    and so does the next rollback (`uvpin.prepare` settles first)."""
    inst = install(tmp_path)
    put_uv(inst.bin / "uv.next", "0.14.0")
    update.update_app(make_zip(tmp_path), inst.app, Env().env(), paths=inst.paths)  # pins no uv
    assert not (inst.bin / "uv.next").exists() and inst.uv() == "0.13.6"
    put_uv(inst.bin / "uv.next", "0.14.0")
    update.rollback_app(inst.app, Env().env(), paths=inst.paths)
    assert not (inst.bin / "uv.next").exists() and inst.uv() == "0.13.6"
