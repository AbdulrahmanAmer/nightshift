"""C1: b02 checks that the copied uv runs, and replaces a shim by the real uv or a fresh download.

Stage 1 hands over whichever `uv.exe` is first on PATH. A Scoop shim (`scoop\\shims\\uv.exe`, with `uv.shim`
beside it) stops working once it is copied away from its folder; b02 used to accept any file at
`bin\\uv\\uv.exe`, so b03 failed with NS-E105 and Retry did the same.
"""

from __future__ import annotations

import hashlib
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from installer.stage2.web import bootstrap
from installer.stage2.web.bootstrap import Boot, BootstrapError, CmdResult

PIN = {"url": "https://example.invalid/uv.zip", "sha256": "a" * 64}


class UvCmd:
    """Runs nothing: a uv file whose bytes contain BROKEN fails `--version`, one with REAL passes."""

    def __init__(self, where: str = "") -> None:
        self.calls: list[list[str]] = []
        self.where = where

    def __call__(
        self, argv: Sequence[str], env: Any = None, cwd: Any = None, timeout: float | None = None
    ) -> CmdResult:
        args = [str(a) for a in argv]
        self.calls.append(args)
        if args[1:] == ["self", "where"]:
            return CmdResult(0, self.where + "\n", "") if self.where else CmdResult(2, "", "unrecognized")
        data = Path(args[0]).read_bytes()
        if args[1:] == ["--version"]:
            if b"BROKEN" in data:
                return CmdResult(1, "", "shim: cannot find uv.shim")
            return CmdResult(0, "uv 0.13.6", "")
        return CmdResult(0, "", "")


class Fetch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path]] = []

    def __call__(self, url: str, sha256: str, dest: Path) -> None:
        self.calls.append((url, sha256, dest))
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "uv.exe").write_bytes(b"MZ REAL downloaded")


def make_boot(
    tmp_path: Path, source: Path, cmd: UvCmd, fetch: Fetch, pin: dict[str, str] | None = None
) -> Boot:
    var: dict[str, str] = {}
    home = tmp_path / "home"
    return Boot(
        app_path=tmp_path / "app", home=home, uv=str(source), cmd=cmd,
        set_home_var=lambda n, v: var.update({n: v}) or True, get_home_var=var.get,
        uv_pin=PIN if pin is None else pin, fetch_uv=fetch,
    )


def scoop_shim(root: Path) -> Path:
    """`scoop\\shims\\uv.exe` + `uv.shim` pointing at `scoop\\apps\\uv\\current\\uv.exe`."""
    real = root / "scoop" / "apps" / "uv" / "current" / "uv.exe"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"MZ REAL " + b"\0" * 10)
    shims = root / "scoop" / "shims"
    shims.mkdir(parents=True)
    exe = shims / "uv.exe"
    exe.write_bytes(b"MZ BROKEN shim stub")
    (shims / "uv.shim").write_text(f'path = "{real}"\nargs =\n', encoding="utf-8")
    return exe


@pytest.fixture(autouse=True)
def home_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIGHTSHIFT_HOME", "placeholder")     # b02 sets it in os.environ: undone at teardown


def run_b02(boot: Boot) -> None:
    step = bootstrap.B02Home()
    assert not step.check(boot)
    step.run(boot)
    step.verify(boot)


def test_a_working_uv_is_copied_and_checked_by_running_it_from_the_copy(tmp_path: Path) -> None:
    src = tmp_path / "boot" / "uv.exe"
    src.parent.mkdir()
    src.write_bytes(b"MZ REAL")
    cmd, fetch = UvCmd(), Fetch()
    boot = make_boot(tmp_path, src, cmd, fetch)
    run_b02(boot)
    copy = str(boot.home / "bin" / "uv" / "uv.exe")
    assert [copy, "--version"] in cmd.calls               # run from the copy, not the source
    assert fetch.calls == []


def test_a_scoop_shim_is_replaced_by_the_exe_its_shim_file_names(tmp_path: Path) -> None:
    src = scoop_shim(tmp_path)
    cmd, fetch = UvCmd(), Fetch()
    boot = make_boot(tmp_path, src, cmd, fetch)
    run_b02(boot)
    assert b"REAL" in (boot.home / "bin" / "uv" / "uv.exe").read_bytes()
    assert fetch.calls == []                                # no download needed


def test_a_shim_without_a_shim_file_is_resolved_with_uv_self_where(tmp_path: Path) -> None:
    real = tmp_path / "tools" / "uv" / "uv.exe"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"MZ REAL")
    src = tmp_path / "choco" / "uv.exe"
    src.parent.mkdir()
    src.write_bytes(b"MZ BROKEN")                           # tiny: a stub, not a 30 MB uv
    cmd, fetch = UvCmd(where=str(real)), Fetch()
    boot = make_boot(tmp_path, src, cmd, fetch)
    run_b02(boot)
    assert b"REAL" in (boot.home / "bin" / "uv" / "uv.exe").read_bytes()
    assert [str(src), "self", "where"] in cmd.calls and fetch.calls == []


def test_a_shim_that_cannot_be_resolved_downloads_the_pinned_uv_afresh(tmp_path: Path) -> None:
    src = tmp_path / "choco" / "uv.exe"
    src.parent.mkdir()
    src.write_bytes(b"MZ BROKEN")
    cmd, fetch = UvCmd(), Fetch()                            # `uv self where` fails
    boot = make_boot(tmp_path, src, cmd, fetch)
    run_b02(boot)
    assert fetch.calls == [(PIN["url"], PIN["sha256"], boot.home / "bin" / "uv")]
    assert b"downloaded" in (boot.home / "bin" / "uv" / "uv.exe").read_bytes()


def test_a_real_looking_uv_that_does_not_run_is_not_treated_as_a_shim_but_downloaded(tmp_path: Path) -> None:
    src = tmp_path / "bin" / "uv.exe"
    src.parent.mkdir()
    src.write_bytes(b"MZ BROKEN" + b"\0" * (bootstrap.SHIM_MAX_BYTES + 10))   # big PE: not a shim
    assert not bootstrap.is_shim(src)
    cmd, fetch = UvCmd(where="never asked"), Fetch()
    boot = make_boot(tmp_path, src, cmd, fetch)
    run_b02(boot)
    assert len(fetch.calls) == 1 and [str(src), "self", "where"] not in cmd.calls


def test_shim_detection_covers_the_three_signs(tmp_path: Path) -> None:
    tiny = tmp_path / "a.exe"
    tiny.write_bytes(b"MZ")
    assert bootstrap.is_shim(tiny)                                           # small
    sibling = tmp_path / "b" / "uv.exe"
    sibling.parent.mkdir()
    sibling.write_bytes(b"MZ" + b"\0" * (bootstrap.SHIM_MAX_BYTES + 1))
    assert not bootstrap.is_shim(sibling)
    sibling.with_suffix(".shim").write_text("path = x", encoding="utf-8")
    assert bootstrap.is_shim(sibling)                                        # .shim sibling
    script = tmp_path / "c.exe"
    script.write_bytes(b"#!" + b"\0" * (bootstrap.SHIM_MAX_BYTES + 1))
    assert bootstrap.is_shim(script)                                         # not a PE


def test_a_broken_copy_from_an_earlier_run_is_not_accepted_and_retry_repairs_it(tmp_path: Path) -> None:
    src = scoop_shim(tmp_path)
    cmd, fetch = UvCmd(), Fetch()
    boot = make_boot(tmp_path, src, cmd, fetch)
    for d in bootstrap.TOP_DIRS:
        (boot.home / d).mkdir(parents=True)
    (boot.home / "config").mkdir(exist_ok=True)
    (boot.home / "config" / "answers.json").write_text("{}", encoding="utf-8")
    boot.set_home_var(bootstrap.HOME_VAR, str(boot.home))
    broken = boot.home / "bin" / "uv" / "uv.exe"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"MZ BROKEN shim stub")                # what the old b02 left behind
    step = bootstrap.B02Home()
    assert step.check(boot) is False                          # it used to say "already done"
    boot.uv = str(broken)                                     # the resolver now prefers the home copy
    step.run(boot)
    step.verify(boot)
    assert len(fetch.calls) == 1 and b"downloaded" in broken.read_bytes()


def test_no_uv_that_runs_and_no_pin_is_a_clear_ns_e105(tmp_path: Path) -> None:
    src = scoop_shim(tmp_path)
    (src.parent / "uv.shim").unlink()
    boot = make_boot(tmp_path, src, UvCmd(), Fetch(), pin={})
    with pytest.raises(BootstrapError) as err:
        bootstrap.B02Home().run(boot)
    assert err.value.code == "NS-E105" and "does not run" in err.value.detail


def test_a_download_that_still_does_not_run_fails_with_ns_e105(tmp_path: Path) -> None:
    src = tmp_path / "choco" / "uv.exe"
    src.parent.mkdir()
    src.write_bytes(b"MZ BROKEN")

    def bad_fetch(url: str, sha256: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "uv.exe").write_bytes(b"MZ BROKEN again")

    boot = make_boot(tmp_path, src, UvCmd(), Fetch())
    boot.fetch_uv = bad_fetch
    with pytest.raises(BootstrapError) as err:
        bootstrap.B02Home().run(boot)
    assert err.value.code == "NS-E105" and "downloaded uv does not run" in err.value.detail


# ------------------------------------------------------------------------------ the real download code
def make_uv_zip(tmp_path: Path) -> tuple[Path, str]:
    z = tmp_path / "uv.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("uv-x86_64-pc-windows-msvc/uv.exe", b"MZ real uv")
        zf.writestr("uv-x86_64-pc-windows-msvc/uvx.exe", b"MZ real uvx")
        zf.writestr("uv-x86_64-pc-windows-msvc/README.md", b"not an exe")
    return z, hashlib.sha256(z.read_bytes()).hexdigest()


def test_download_uv_checks_the_digest_and_extracts_the_executables(tmp_path: Path) -> None:
    z, digest = make_uv_zip(tmp_path)
    dest = tmp_path / "home" / "bin" / "uv"
    bootstrap.download_uv(z.as_uri(), digest, dest)
    assert sorted(p.name for p in dest.iterdir()) == ["uv.exe", "uvx.exe"]   # no zip or readme left behind
    assert (dest / "uv.exe").read_bytes() == b"MZ real uv"


def test_download_uv_refuses_a_wrong_digest_and_leaves_nothing(tmp_path: Path) -> None:
    z, _ = make_uv_zip(tmp_path)
    dest = tmp_path / "home" / "bin" / "uv"
    with pytest.raises(BootstrapError) as err:
        bootstrap.download_uv(z.as_uri(), "0" * 64, dest)
    assert err.value.code == "NS-E105" and "SHA256" in err.value.detail
    assert list(dest.iterdir()) == []


def test_download_uv_reports_a_missing_file_as_ns_e105(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError) as err:
        bootstrap.download_uv((tmp_path / "nope.zip").as_uri(), "0" * 64, tmp_path / "d")
    assert err.value.code == "NS-E105"


def test_the_setup_server_passes_the_manifest_pin_and_the_fetcher_to_the_steps(tmp_path: Path) -> None:
    from installer.stage2.web.fakes import fake_effects

    fx, _cmd = fake_effects(tmp_path)
    assert fx.fetch_uv is not bootstrap.download_uv          # simulate mode never reaches the network
