"""D-19: setup places only the uv the manifest pins (``bins.uv.version``), whatever is on PATH.

QA found winget's uv 0.13.7 in ``<home>\\bin\\uv`` although the manifest pins 0.13.6: the controller's uv
comes from ``find_uv`` (home copy, bootstrap folder, then ``shutil.which``) and b02 only checked that it ran.
Every test ends at the effect: which uv file ends up in ``<home>\\bin\\uv`` and the version it reports.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from installer.stage2.web import bootstrap, controller
from installer.stage2.web.bootstrap import Boot, BootstrapError, CmdResult
from ns_common import uv as uv_resolver
from tests.test_installer_web_controller import Setup

PINNED = "0.13.6"
WINGET = "0.13.7"
SAID: list[str] = []  # what setup said in the window (b02 reports which uv it uses)


def uv_bytes(version: str) -> bytes:
    """A stand-in uv.exe: ``MZ`` plus the version it will report (a real one is tens of MB)."""
    return f"MZ uv={version}".encode()


class VersionCmd:
    """Runs no program: ``--version`` answers with the version written inside the file that was asked."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: Sequence[str], env: Any = None, cwd: Any = None, timeout: float | None = None
    ) -> CmdResult:
        args = [str(a) for a in argv]
        self.calls.append(args)
        if args[1:] == ["--version"]:
            found = re.search(rb"uv=([^\s\0]+)", Path(args[0]).read_bytes())
            if found is None:
                return CmdResult(1, "", "not a uv")
            return CmdResult(0, f"uv {found.group(1).decode()} (abc1234 2025-11-01)\n", "")
        return CmdResult(0, "", "")


def pinned_zip(tmp_path: Path, version: str = PINNED) -> tuple[Path, str]:
    """A release zip of the shape astral publishes (uv.exe, uvx.exe, uvw.exe) and its SHA256."""
    z = tmp_path / "transport" / "uv-x86_64-pc-windows-msvc.zip"
    z.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(z, "w") as zf:
        for exe in ("uv.exe", "uvx.exe", "uvw.exe"):
            zf.writestr(exe, uv_bytes(version))
    return z, hashlib.sha256(z.read_bytes()).hexdigest()


class Transport:
    """The real ``download_uv`` (hash check and unpack) over a file:// URL, counting downloads."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url: str, sha256: str, dest: Path) -> None:
        self.urls.append(url)
        bootstrap.download_uv(url, sha256, dest)


def make_boot(
    tmp_path: Path, source: Path, cmd: VersionCmd, transport: Transport, pin: dict[str, str]
) -> Boot:
    var: dict[str, str] = {}
    boot = Boot(
        app_path=tmp_path / "app",
        home=tmp_path / "home",
        uv=str(source),
        cmd=cmd,
        set_home_var=lambda n, v: var.update({n: v}) or True,
        get_home_var=var.get,
        uv_pin=pin,
        fetch_uv=transport,
        say=SAID.append,
    )
    return boot


def pin_for(tmp_path: Path, version: str = PINNED, zip_version: str | None = None) -> dict[str, str]:
    z, digest = pinned_zip(tmp_path, zip_version or version)
    return {"version": version, "url": z.as_uri(), "sha256": digest}


def path_uv(tmp_path: Path, version: str) -> Path:
    """The uv found on PATH (winget's Links folder): what ``find_uv`` hands setup when nothing else exists."""
    exe = tmp_path / "WinGet" / "Links" / "uv.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(
        uv_bytes(version) + b"\0" * (bootstrap.SHIM_MAX_BYTES + 1)
    )  # a real-sized exe, not a shim
    return exe


def home_uv(boot: Boot, name: str = "uv.exe") -> Path:
    return boot.home / "bin" / "uv" / name


def reported(boot: Boot, cmd: VersionCmd, name: str = "uv.exe") -> str:
    res = cmd([str(home_uv(boot, name)), "--version"])
    assert res.returncode == 0
    return res.stdout.split()[1]


@pytest.fixture(autouse=True)
def home_var(monkeypatch: pytest.MonkeyPatch) -> None:
    SAID.clear()
    monkeypatch.setenv("NIGHTSHIFT_HOME", "placeholder")  # b02 sets it in os.environ: undone at teardown


def run_b02(boot: Boot) -> None:
    step = bootstrap.B02Home()
    assert not step.check(boot)
    step.run(boot)
    step.verify(boot)
    assert step._uv_is_pinned(boot)


def test_a_path_uv_of_another_version_is_not_copied_and_the_pin_is_downloaded(tmp_path: Path) -> None:
    cmd, net = VersionCmd(), Transport()
    boot = make_boot(tmp_path, path_uv(tmp_path, WINGET), cmd, net, pin_for(tmp_path))
    run_b02(boot)
    assert reported(boot, cmd) == PINNED  # the file in <home>\bin\uv is the pin, not winget's
    assert home_uv(boot).read_bytes() == uv_bytes(PINNED)
    assert len(net.urls) == 1
    assert any(WINGET in line and PINNED in line for line in SAID)  # the version is logged


def test_the_whole_uv_family_in_the_install_folder_is_the_pinned_one(tmp_path: Path) -> None:
    src = path_uv(tmp_path, WINGET)
    (src.parent / "uvx.exe").write_bytes(uv_bytes(WINGET))
    (src.parent / "uvw.exe").write_bytes(uv_bytes(WINGET))
    cmd, net = VersionCmd(), Transport()
    boot = make_boot(tmp_path, src, cmd, net, pin_for(tmp_path))
    run_b02(boot)
    for name in ("uv.exe", "uvx.exe", "uvw.exe"):
        assert home_uv(boot, name).read_bytes() == uv_bytes(PINNED)


def test_a_path_uv_of_the_pinned_version_is_copied_without_a_download(tmp_path: Path) -> None:
    cmd, net = VersionCmd(), Transport()
    boot = make_boot(tmp_path, path_uv(tmp_path, PINNED), cmd, net, pin_for(tmp_path))
    run_b02(boot)
    assert reported(boot, cmd) == PINNED
    assert net.urls == []
    assert home_uv(boot).read_bytes().startswith(uv_bytes(PINNED))  # the copy of the source, not a download


def test_a_wrong_version_already_in_the_install_folder_is_replaced_by_the_pin(tmp_path: Path) -> None:
    """QA's case: setup ran again, ``find_uv`` returns the home copy and it is winget's 0.13.7."""
    cmd, net = VersionCmd(), Transport()
    boot = make_boot(tmp_path, tmp_path / "unused" / "uv.exe", cmd, net, pin_for(tmp_path))
    stale = home_uv(boot)
    stale.parent.mkdir(parents=True)
    for name in ("uv.exe", "uvx.exe"):
        (stale.parent / name).write_bytes(uv_bytes(WINGET) + b"\0" * (bootstrap.SHIM_MAX_BYTES + 1))
    boot.uv = str(stale)  # the resolver prefers the home copy
    for d in bootstrap.TOP_DIRS:
        (boot.home / d).mkdir(parents=True, exist_ok=True)
    (boot.home / "config" / "answers.json").write_text("{}", encoding="utf-8")
    boot.set_home_var(bootstrap.HOME_VAR, str(boot.home))
    step = bootstrap.B02Home()
    assert step._uv_is_pinned(boot) is False  # it runs, but it is not the pin: not "done"
    step.run(boot)
    step.verify(boot)
    assert reported(boot, cmd) == PINNED
    assert home_uv(boot, "uvx.exe").read_bytes() == uv_bytes(PINNED)
    assert len(net.urls) == 1


def test_a_download_that_fails_its_hash_leaves_no_wrong_uv_in_the_install_folder(tmp_path: Path) -> None:
    pin = pin_for(tmp_path)
    pin["sha256"] = "0" * 64
    cmd, net = VersionCmd(), Transport()
    boot = make_boot(tmp_path, path_uv(tmp_path, WINGET), cmd, net, pin)
    with pytest.raises(BootstrapError) as err:
        bootstrap.B02Home().run(boot)
    assert err.value.code == "NS-E105" and "SHA256" in err.value.detail
    assert not any(p.name.lower().startswith("uv") for p in home_uv(boot).parent.glob("*.exe"))


def test_a_download_of_another_version_than_the_pin_is_refused(tmp_path: Path) -> None:
    pin = pin_for(
        tmp_path, version=PINNED, zip_version=WINGET
    )  # the zip holds 0.13.7 though the hash matches
    cmd, net = VersionCmd(), Transport()
    boot = make_boot(tmp_path, path_uv(tmp_path, WINGET), cmd, net, pin)
    with pytest.raises(BootstrapError) as err:
        bootstrap.B02Home().run(boot)
    assert err.value.code == "NS-E105" and PINNED in err.value.detail and WINGET in err.value.detail


def test_without_a_download_a_wrong_version_is_a_clear_ns_e105_naming_both_versions(tmp_path: Path) -> None:
    cmd, net = VersionCmd(), Transport()
    boot = make_boot(tmp_path, path_uv(tmp_path, WINGET), cmd, net, {"version": PINNED})
    with pytest.raises(BootstrapError) as err:
        bootstrap.B02Home().run(boot)
    assert err.value.code == "NS-E105" and PINNED in err.value.detail and WINGET in err.value.detail
    assert net.urls == []
    assert not home_uv(boot).exists()  # the unpinned copy was not left behind


def test_verify_refuses_a_install_folder_uv_of_another_version(tmp_path: Path) -> None:
    cmd, net = VersionCmd(), Transport()
    boot = make_boot(tmp_path, path_uv(tmp_path, PINNED), cmd, net, pin_for(tmp_path))
    run_b02(boot)
    home_uv(boot).write_bytes(uv_bytes(WINGET))  # winget's copy lands there later
    with pytest.raises(BootstrapError) as err:
        bootstrap.B02Home().verify(boot)
    assert err.value.code == "NS-E105" and WINGET in err.value.detail


def test_a_manifest_pin_without_a_version_still_accepts_a_uv_that_runs(tmp_path: Path) -> None:
    pin = pin_for(tmp_path)
    del pin["version"]  # nothing to compare with: the old rule applies
    cmd, net = VersionCmd(), Transport()
    boot = make_boot(tmp_path, path_uv(tmp_path, WINGET), cmd, net, pin)
    run_b02(boot)
    assert reported(boot, cmd) == WINGET and net.urls == []


# --- the stage-1 hand-over: the pinned uv stage 1 put in the bootstrap folder is looked at before a download
class Unreachable:
    """GitHub is down: every download attempt is counted and fails the way ``download_uv`` fails."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url: str, sha256: str, dest: Path) -> None:
        self.urls.append(url)
        raise BootstrapError("NS-E105", f"could not download uv from {url}: timed out")


def stage1_handover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str = PINNED) -> Path:
    """What stage 1 leaves: a uv in the bootstrap folder, named in the hand-over env var."""
    local = tmp_path / "lad"
    exe = local / "Nightshift" / "bootstrap" / "uv" / "uv.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(uv_bytes(version) + b"\0" * (bootstrap.SHIM_MAX_BYTES + 1))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv(uv_resolver.BOOTSTRAP_ENV, str(exe))
    return exe


def controller_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cmd: VersionCmd, net: Any, version: str, stale: str
) -> Boot:
    """The ``Boot`` the setup controller makes: ``uv`` comes from the real ``default_uv_path`` -> ``find_uv``.

    The install folder already holds a uv of version ``stale`` (QA's winget copy), so ``find_uv`` returns it.
    """
    exe = tmp_path / "home" / "bin" / "uv" / "uv.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(uv_bytes(stale) + b"\0" * (bootstrap.SHIM_MAX_BYTES + 1))
    s = Setup(tmp_path, monkeypatch)
    s.fx.uv_path = controller.default_uv_path
    s.fx.handover_uv = uv_resolver.bootstrap_uv  # the real resolver, reading the environment
    s.fx.cmd = cmd
    s.fx.fetch_uv = net
    s.ctl.manifest.setdefault("bins", {})["uv"] = pin_for(tmp_path, version)
    boot = s.ctl._boot()
    assert boot.uv == str(
        boot.home / "bin" / "uv" / "uv.exe"
    )  # find_uv answered with the install folder copy
    return boot


def test_the_stage1_pinned_uv_replaces_a_wrong_install_folder_copy_without_a_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA's machine: home bin/uv holds 0.13.7, stage 1 refused it and handed over 0.13.6; GitHub is down."""
    handed = stage1_handover(tmp_path, monkeypatch)
    cmd, net = VersionCmd(), Unreachable()
    boot = controller_boot(tmp_path, monkeypatch, cmd, net, PINNED, stale=WINGET)
    step = bootstrap.B02Home()
    assert step._uv_is_pinned(boot) is False
    step._place_uv(boot)
    assert net.urls == []  # nothing was downloaded
    assert reported(boot, cmd) == PINNED
    assert home_uv(boot).read_bytes() == handed.read_bytes()


def test_the_pin_is_downloaded_only_after_the_install_folder_and_the_stage1_uv_were_both_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage1_handover(tmp_path, monkeypatch, version=WINGET)  # the hand-over is not the pin either
    cmd, net = VersionCmd(), Unreachable()
    boot = controller_boot(tmp_path, monkeypatch, cmd, net, PINNED, stale=WINGET)
    with pytest.raises(BootstrapError) as err:
        bootstrap.B02Home()._place_uv(boot)
    assert err.value.code == "NS-E105" and "could not download" in err.value.detail
    assert len(net.urls) == 1  # the pin was tried only after both local copies were found wrong


def test_a_uv_that_cannot_be_deleted_is_a_clear_ns_e105_not_an_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, net = VersionCmd(), Transport()
    boot = make_boot(tmp_path, path_uv(tmp_path, WINGET), cmd, net, pin_for(tmp_path))
    target = home_uv(boot)
    target.parent.mkdir(parents=True)
    target.write_bytes(uv_bytes(WINGET))

    def locked(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(13, "The process cannot access the file because it is being used", str(self))

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(BootstrapError) as err:
        bootstrap.B02Home()._place_uv(boot)
    assert err.value.code == "NS-E105" and "uv.exe" in err.value.detail and "in use" in err.value.detail
