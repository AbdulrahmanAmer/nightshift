"""Card update-signing: `dev/sign_release.py`, the signer of the release run.

It signs `Nightshift-Update-<v>.zip` and `Nightshift-Setup-<v>.zip` with the key of the secret
`NS_RELEASE_SIGNING_KEY`, runs the shipped verifier on the result with the keys the tagged commit pins, and
decides what a run without the key does. A release is signed with `cryptography`, which no Windows test
machine has (`uv.lock` carries it for Linux only): the tests give the signer the standard library's reference
code instead, whose bytes are the same (Ed25519 is deterministic).
"""
# ruff: noqa: E501

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import re
from pathlib import Path

import pytest

from tests.test_update_signing import (
    ROOT,
    TEST_SEED,
    _module,
    key_of,
    package,
    release_keys,
    sig_of,
    sigcheck,
)

sign_release = _module("dev.sign_release")
REAL_SIGNER = getattr(sign_release, "real_signer", None)  # (the tests below put the reference signer in its place)
KEY_ENV = "NS_RELEASE_SIGNING_KEY"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    code = sign_release.main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


def signed_by(z: Path, keys: dict[str, str]) -> str:
    """The key the product's own check finds for the zip (`BadSignature` when none of `keys` signed it)."""
    return sigcheck.check_package(z, sigcheck.package_version(z)[0], hashlib.sha256(z.read_bytes()).hexdigest(), keys)


@pytest.fixture
def zips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The two packages of a release run, no key in the environment, and the reference signer."""
    if sign_release is None or release_keys is None:
        return []  # (the base: each test says so at its first assertion)
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr(release_keys, "TRUSTED", {})
    monkeypatch.setattr(sign_release, "real_signer", sign_release.reference_signer)
    return [package(tmp_path, name=f"Nightshift-{kind}-0.1.16.zip") for kind in ("Update", "Setup")]


def test_the_sig_file_has_the_format_and_no_key_is_printed(
    zips: list[Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert zips, "dev/sign_release.py is missing"
    z = zips[0]
    sign_release.sign_zip(z, sign_release.reference_signer(TEST_SEED))  # a throwaway key: no wheel needed
    raw = sig_of(z).read_bytes()
    assert re.fullmatch(rb"alg: ed25519\nkey: [0-9a-f]{16}\nsig: [A-Za-z0-9+/]{86}==\n", raw), raw
    assert signed_by(z, dict([key_of(TEST_SEED)])) == key_of(TEST_SEED)[0]
    assert not hasattr(sign_release, "TEST_KEY_FILE")  # the repository holds no key the script could sign with
    secret = os.urandom(32)
    monkeypatch.setenv(KEY_ENV, b64(secret) + "\r\n")  # as the release run gives it; a line end is dropped
    code, said = run(capsys, str(z))
    assert code == 0 and signed_by(z, dict([key_of(secret)])) == key_of(secret)[0]
    assert b64(secret) not in said and secret.hex() not in said and key_of(secret)[0] in said
    sig_of(z).unlink()
    for bad in (b64(os.urandom(31)), b64(os.urandom(64)), "not base64 at all !"):  # not a key of 32 bytes
        monkeypatch.setenv(KEY_ENV, bad)
        code, said = run(capsys, str(z))
        assert code == 1 and "32 bytes" in said and bad not in said and not sig_of(z).exists(), said
    monkeypatch.delenv(KEY_ENV)
    code, said = run(capsys, str(z))
    assert code == 1 and KEY_ENV in said and not sig_of(z).exists()  # no key at all: said, nothing written
    if importlib.util.find_spec("cryptography") is None:  # without the pinned wheel the signer says what to install
        with pytest.raises(sign_release.Refused, match="sign-requirements.txt"):
            REAL_SIGNER(secret)
    else:  # with it: the same bytes as the reference code
        signed, real = sigcheck.message(z.name, "0.1.16", "0" * 64), REAL_SIGNER(secret)
        assert real.sign(signed) == sigcheck.sign(secret, signed) and real.public == sigcheck.public_key(secret)


def test_the_release_mode_fails_a_final_without_the_key_and_lets_a_candidate_go_unsigned(
    zips: list[Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert zips, "dev/sign_release.py is missing"
    names, summary = [str(z) for z in zips], tmp_path / "summary.md"
    code, said = run(capsys, "--release", "v0.1.16-rc1", *names)  # a candidate before the owner's step
    assert code == 0 and "::warning::" in said and "UNSIGNED" in summary.read_text(encoding="utf-8")
    assert not any(sig_of(z).exists() for z in zips)
    code, said = run(capsys, "--release", "v0.1.16", *names)  # a final tag: the run stops at the owner's one step
    assert code == 1 and "::error::The release key is not set up: run dev\\release_keys.ps1 -Create once" in said
    assert '"The release key"' in said and not any(sig_of(z).exists() for z in zips)
    seed = os.urandom(32)
    monkeypatch.setattr(release_keys, "TRUSTED", dict([key_of(seed)]))  # a commit that pins keys, the secret gone
    code, said = run(capsys, "--release", "v0.1.16-rc2", *names)
    assert code == 1 and '"Lost key"' in said and not any(sig_of(z).exists() for z in zips)
    monkeypatch.setenv(KEY_ENV, b64(seed))  # the key is there and pinned: both zips signed, then checked
    made: list[list[bytes]] = []
    for _run in (1, 2):  # a second run of the step writes the same bytes
        code, said = run(capsys, "--release", "v0.1.16", *names)
        assert code == 0 and [signed_by(z, release_keys.TRUSTED) for z in zips] == [key_of(seed)[0]] * 2, said
        assert b64(seed) not in said
        made.append([sig_of(z).read_bytes() for z in zips])
    assert made[0] == made[1] and made[0][0] != made[0][1]  # the name is signed: each zip has its own .sig


def test_a_key_that_is_not_pinned_fails_the_run_and_leaves_no_sig(
    zips: list[Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert zips, "dev/sign_release.py is missing"
    seed, other = os.urandom(32), os.urandom(32)
    monkeypatch.setenv(KEY_ENV, b64(seed))
    for pinned in ({}, dict([key_of(other)])):  # the public keys are not committed yet; or the secret was replaced
        monkeypatch.setattr(release_keys, "TRUSTED", pinned)
        code, said = run(capsys, "--release", "v0.1.16-rc1", *[str(z) for z in zips])
        assert code == 1 and key_of(seed)[0] in said and "ns_common/release_keys.py of this commit pins" in said
        assert not any(sig_of(z).exists() for z in zips) and b64(seed) not in said
    monkeypatch.setattr(release_keys, "TRUSTED", dict([key_of(seed)]))
    code, said = run(capsys, "--check", str(zips[0]))  # the check alone, on a zip with no .sig
    assert code == 1 and "missing" in said


def test_the_signing_requirements_are_pinned_from_the_lock() -> None:
    wanted = ROOT / "dev" / "sign-requirements.txt"
    assert wanted.is_file(), "dev/sign-requirements.txt is missing"
    text, lock = wanted.read_text(encoding="utf-8"), (ROOT / "uv.lock").read_text(encoding="utf-8")
    pins = dict(re.findall(r"(?m)^([a-z0-9-]+)==(\S+)", text))
    assert set(pins) == {"cryptography", "cffi", "pycparser"}  # the package and all it needs, nothing unpinned
    for name, version in pins.items():
        assert f'name = "{name}"\nversion = "{version}"' in lock, name  # the versions the project's lock holds
    hashed = dict(re.findall(r"(?m)^([a-z0-9-]+)==\S+ \\\n((?:\s+--hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", text))
    assert set(hashed) == set(pins)  # each pin carries a hash: pip refuses the file otherwise
    for name in ("cffi", "pycparser"):  # the lock's own hashes (it has cryptography's Linux wheels only)
        assert all(f"sha256:{h}" in lock for h in re.findall(r"[0-9a-f]{64}", hashed[name])), name
    assert "\n--only-binary :all:\n" in text and "win_amd64" in text  # never built from a source archive


def test_a_byte_order_mark_before_the_key_is_not_part_of_the_key() -> None:
    """Windows PowerShell 5.1 on a UTF-8 console (code page 65001) writes EF BB BF before what it pipes to a
    program: `dev/release_keys.ps1 -Create` then got "not base64 of 32 bytes" from `--public-key` although the
    key was whole (seen on the laptop, 2026-11-11). The same for a key file an editor saved with the mark."""
    import subprocess
    import sys

    assert sign_release is not None, "dev/sign_release.py is missing"
    key = b64(TEST_SEED).encode("ascii")
    want = " ".join(key_of(TEST_SEED))
    script = str(ROOT / "dev" / "sign_release.py")
    for given in (key + b"\r\n", b"\xef\xbb\xbf" + key + b"\r\n"):
        done = subprocess.run([sys.executable, script, "--public-key"], input=given, capture_output=True, timeout=120, check=False)
        said = done.stdout.decode("ascii", errors="replace").strip()
        assert (done.returncode, said) == (0, want), (given[:3], done.stdout, done.stderr)
    assert sign_release.read_key("﻿# the backup key\n" + key.decode("ascii") + "\n") == TEST_SEED
