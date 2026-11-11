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
