"""Sign the packages of a release with the release key, and check what was signed (card update-signing).

    python dev/sign_release.py --release <tag> <zip> [<zip> ...]   the release run's "Sign" step
    python dev/sign_release.py [--key-file <file>] <zip> [<zip> ...]
    python dev/sign_release.py --check <zip> [<zip> ...]
    python dev/sign_release.py --public-key                        (a private key on stdin)

Signing writes `<zip>.sig` beside each zip (`ns_common.sigcheck`: the format, and what is signed). The private
key is base64 of 32 bytes: the variable `NS_RELEASE_SIGNING_KEY` (the GitHub secret, given to the Sign step
only), or the first line of `--key-file` that is not a `#` comment (the owner's backup key, OPERATOR.md "Lost
key"). It is never printed, and neither is anything derived from it but the public key's id. A release is
signed with `cryptography` (`pip install --require-hashes -r dev/sign-requirements.txt`). The repository
holds no key of any kind: tests and proofs make a throwaway key and sign with `reference_signer`.

`--release` is the whole step. It decides what a run without the key does (a candidate of a commit that pins
no key is published unsigned, with a warning; a final tag, and any tag of a commit that pins keys, fails),
signs, and then runs the shipped verifier with the keys the checkout pins (`ns_common/release_keys.py`): a
package signed with a key no installed Nightshift could check fails the run and leaves no `.sig` behind.
`--check` is that last part alone. `--public-key` is the helper of `dev/release_keys.ps1`: the key's id and
public half, after a signature made with it has verified.

Standard library and `ns_common.sigcheck` only, plus `cryptography` for a real signature: the release job
installs nothing else.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # started as a script: the checkout this file is in
    sys.path.insert(0, str(ROOT))

from ns_common import release_keys, sigcheck  # noqa: E402
from ns_common.atomic import write_bytes_atomic  # noqa: E402

KEY_ENV = "NS_RELEASE_SIGNING_KEY"
NO_KEY_FINAL = (
    'The release key is not set up: run dev\\release_keys.ps1 -Create once (OPERATOR.md, "The release key").'
)
NO_KEY_PINNED = (
    f"This commit pins release keys in ns_common/release_keys.py, but the secret {KEY_ENV} is not set, and "
    "every install that has those keys would refuse an unsigned package. Set the secret again from the "
    'backup key (OPERATOR.md, "Lost key"), then tag again.'
)
UNSIGNED_CANDIDATE = (
    f"This candidate is published UNSIGNED: the release key is not set up yet (the secret {KEY_ENV} is empty "
    "and ns_common/release_keys.py pins no key). An install accepts it on its SHA256 alone. Before a final "
    'release, run dev\\release_keys.ps1 -Create once (OPERATOR.md, "The release key").'
)
NOT_PINNED = (
    "{name} was signed, but not with a key that ns_common/release_keys.py of this commit pins (key {key}): "
    "no installed Nightshift could check it, so no .sig was kept. Commit the release_keys.py that "
    'dev\\release_keys.ps1 wrote, or see "Lost key" in OPERATOR.md.'
)


class Refused(Exception):
    """A sentence for whoever runs this; never carries anything of a key."""


@dataclass(frozen=True)
class Signer:
    public: bytes
    sign: Callable[[bytes], bytes]


def read_key(text: str) -> bytes:
    """The 32 bytes of a private key given as base64 (a secret's value, or a key file with `#` comments).
    `Refused`, without the value, for anything else."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    try:
        seed = base64.b64decode(lines[0], validate=True) if len(lines) == 1 else b""
    except (binascii.Error, ValueError):
        seed = b""
    if len(seed) != 32:
        raise Refused(
            f"The release key is not base64 of 32 bytes (it decodes to {len(seed)} bytes). Nothing was "
            f"signed. Set {KEY_ENV} to the value dev\\release_keys.ps1 made, or pass the backup key file."
        )
    return seed


def real_signer(seed: bytes) -> Signer:
    """The signer of a release: `cryptography`'s Ed25519."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as e:
        raise Refused(
            "The package 'cryptography' is not installed, so nothing was signed: "
            "pip install --require-hashes -r dev/sign-requirements.txt"
        ) from e
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return Signer(key.public_key().public_bytes_raw(), key.sign)


def reference_signer(seed: bytes) -> Signer:
    """The standard library's reference code (`sigcheck.sign`): for throwaway keys in tests. Same bytes."""
    return Signer(sigcheck.public_key(seed), lambda message: sigcheck.sign(seed, message))


def sign_zip(zip_path: Path, signer: Signer) -> Path:
    """Write `<zip>.sig` (whole, or not at all) and return it. `Refused` when the zip is not a package."""
    try:
        version, _prefix = sigcheck.package_version(zip_path)
        signed = sigcheck.message(zip_path.name, version, sigcheck.sha256_file(zip_path))
    except (OSError, ValueError) as e:
        raise Refused(f"{zip_path} cannot be signed: {e}") from e
    text = sigcheck.sig_text(signer.public, signer.sign(signed))
    return write_bytes_atomic(sigcheck.sig_path(zip_path), text.encode("ascii"))


def unverified(zips: list[Path], keys: dict[str, str]) -> list[str]:
    """One sentence for each zip that the shipped check does not accept with `keys`; empty: all pass."""
    problems = []
    for z in zips:
        try:
            version, _prefix = sigcheck.package_version(z)
            sigcheck.check_package(z, version, sigcheck.sha256_file(z), keys)
        except sigcheck.BadSignature as e:
            if e.kind == "unknown_key":
                problems.append(NOT_PINNED.format(name=z.name, key=e.key))
            else:
                problems.append(f"{sigcheck.sig_path(z).name} does not verify ({e}).")
        except (OSError, ValueError) as e:
            problems.append(f"{z} cannot be checked: {e}")
    return problems


def _annotate(kind: str, text: str, say: Callable[[str], None]) -> None:
    """A line the run's log shows as an annotation, and a paragraph of the run summary when there is one."""
    say(f"::{kind}::{text}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"**Sign ({kind}).** {text}\n\n")


def release(tag: str, zips: list[Path], say: Callable[[str], None]) -> int:
    """The Sign step of the release run for `tag`: 0 to go on to Publish, 1 to stop the run."""
    raw = os.environ.get(KEY_ENV, "").strip()
    pinned = dict(release_keys.TRUSTED)
    if not raw:
        if pinned or "-" not in tag:  # a final tag (vX.Y.Z), or a version line that already has keys
            _annotate("error", NO_KEY_PINNED if pinned else NO_KEY_FINAL, say)
            return 1
        _annotate("warning", UNSIGNED_CANDIDATE, say)
        return 0
    made: list[Path] = []
    key = ""
    try:
        signer = real_signer(read_key(raw))
        key = sigcheck.key_id(signer.public)
        for z in zips:
            made.append(sign_zip(z, signer))
        problems = unverified(zips, pinned)
    except Refused as e:
        problems = [str(e)]
    if problems:
        for sig in (*made, *(sigcheck.sig_path(z) for z in zips)):
            sig.unlink(missing_ok=True)  # Publish uploads every .sig it finds: none may be left
        for line in problems:
            _annotate("error", line, say)
        return 1
    say(f"Signed {', '.join(z.name for z in zips)} with release key {key}; the shipped check accepts each.")
    return 0


def _public_key(say: Callable[[str], None]) -> int:
    """`<key id> <public key>` of the private key on stdin, after a signature made with it has verified."""
    seed = read_key(sys.stdin.read())
    public = sigcheck.public_key(seed)
    probe = sigcheck.message("probe.zip", "0", "0" * 64)
    if not sigcheck.verify(public, probe, sigcheck.sign(seed, probe)):
        raise Refused("The key pair does not verify its own signature. Nothing was changed.")
    say(f"{sigcheck.key_id(public)} {base64.b64encode(public).decode('ascii')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="sign the packages of a release, or check their signatures")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--release", metavar="TAG", help="the release run's Sign step for this tag")
    mode.add_argument("--check", action="store_true", help="verify <zip>.sig with the keys the checkout pins")
    mode.add_argument("--public-key", action="store_true", help="a private key on stdin: its id, public key")
    mode.add_argument("--key-file", type=Path, help="sign with the private key in this file (the backup)")
    ap.add_argument("zips", nargs="*", type=Path)
    args = ap.parse_args(argv)

    def say(line: str) -> None:
        sys.stdout.write(line + "\n")

    try:
        if args.public_key:
            return _public_key(say)
        if not args.zips:
            raise Refused("Name at least one zip.")
        if args.release:
            return release(args.release, args.zips, say)
        if args.check:
            problems = unverified(args.zips, dict(release_keys.TRUSTED))
            for line in problems or [f"{z.name}: signed with a key this checkout pins." for z in args.zips]:
                say(line)
            return 1 if problems else 0
        if args.key_file is not None:
            signer = real_signer(read_key(args.key_file.read_text(encoding="utf-8")))
        elif os.environ.get(KEY_ENV, "").strip():
            signer = real_signer(read_key(os.environ[KEY_ENV]))
        else:
            raise Refused(f"No key: set {KEY_ENV}, or pass --key-file. Nothing was signed.")
        for z in args.zips:
            say(f"Wrote {sign_zip(z, signer)} (key {sigcheck.key_id(signer.public)}).")
        return 0
    except (Refused, OSError) as e:
        sys.stderr.write(f"{e}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
