"""Who made an update package: the Ed25519 signature beside it (card update-signing). Standard library only.

A release run signs `Nightshift-Update-<v>.zip` and `Nightshift-Setup-<v>.zip` with the owner's release key
and publishes `<zip>.sig` beside each: three ASCII lines, `alg: ed25519`, `key: <key id>`, `sig: <base64 of
the 64 signature bytes>`. The key id is the first 16 hex digits of the SHA-256 of the 32-byte public key. The
signed bytes are `message`: a fixed first line, the zip's file name, the `VERSION` inside it and its SHA-256,
so a signature cannot be moved to another file, another name or another version.

`check_package` is the one check: `ns_supervisor.update.verify_package` calls it with the keys the installed
program trusts (`ns_common.release_keys`), and the release run calls it on what it has just signed. The
arithmetic is RFC 8032's reference code (section 6) with `hashlib.sha512`: the check runs in the OLD
program's updater and in a release job with nothing installed, so it must not need a wheel.

`sign` and `public_key` are the same reference code. They are not constant time: they exist for the TEST key
(tests, the fake GitHub of the proofs) and to derive a public key once on the owner's own PC
(`dev/release_keys.ps1`). A release is signed with `cryptography` (`dev/sign_release.py`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path

ALG = "ed25519"
CONTEXT = "nightshift-package-v1"
SIG_SUFFIX = ".sig"
#: A `.sig` is about 130 bytes; a longer file is not one (and is never read into memory whole).
SIG_MAX_BYTES = 1024
_SIG_RE = re.compile(r"alg: ed25519\r?\nkey: ([0-9a-f]{16})\r?\nsig: ([A-Za-z0-9+/]{86}==)\r?\n?")

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, -1, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_Point = tuple[int, int, int, int]


def _add(p: _Point, q: _Point) -> _Point:
    a, b = (p[1] - p[0]) * (q[1] - q[0]) % _P, (p[1] + p[0]) * (q[1] + q[0]) % _P
    c, d = 2 * p[3] * q[3] * _D % _P, 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _mul(s: int, p: _Point) -> _Point:
    q: _Point = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, -1, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    return _P - x if (x & 1) != sign else x


_GY = 4 * pow(5, -1, _P) % _P
_GX = _recover_x(_GY, 0) or 0
_G: _Point = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(p: _Point) -> bytes:
    zinv = pow(p[2], -1, _P)
    x, y = p[0] * zinv % _P, p[1] * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decompress(s: bytes) -> _Point | None:
    y = int.from_bytes(s, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def _hash(*parts: bytes) -> int:
    return int.from_bytes(hashlib.sha512(b"".join(parts)).digest(), "little") % _L


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True when `signature` (64 bytes) is `public`'s (32 bytes) Ed25519 signature of `message` (RFC 8032
    section 5.1.7). A key or signature of another length, a point that is not on the curve and an `S` that
    is not below the group order are False, never an exception."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a, r = _decompress(public), _decompress(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _L:
        return False
    p, q = _mul(s, _G), _add(r, _mul(_hash(signature[:32], public, message), a))
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _expand(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError(f"a private key is 32 bytes, not {len(seed)}")
    h = hashlib.sha512(seed).digest()
    return (int.from_bytes(h[:32], "little") & ((1 << 254) - 8)) | (1 << 254), h[32:]


def public_key(seed: bytes) -> bytes:
    """The 32-byte public key of the private key `seed` (32 bytes). Reference code: see the module text."""
    return _compress(_mul(_expand(seed)[0], _G))


def sign(seed: bytes, message: bytes) -> bytes:
    """The 64-byte signature of `message` with the private key `seed`. Reference code, not constant time:
    for the TEST key and for a self-check on the owner's PC only (see the module text)."""
    a, prefix = _expand(seed)
    public = _compress(_mul(a, _G))
    r = _hash(prefix, message)
    rs = _compress(_mul(r, _G))
    return rs + ((r + _hash(rs, public, message) * a) % _L).to_bytes(32, "little")


# ------------------------------------------------------------------------------------ the package format
def key_id(public: bytes) -> str:
    """How a `.sig`, a message and `release_keys` name a public key."""
    return hashlib.sha256(public).hexdigest()[:16]


def message(name: str, version: str, sha256: str) -> bytes:
    """The bytes that are signed for the package `name` holding `version` with the digest `sha256`.
    `ValueError` for a part with a line break or none at all: the lines must stay three parts."""
    for part in (name, version, sha256):
        if not part or part != part.strip() or any(c in part for c in "\r\n"):
            raise ValueError("a package name, version or digest is empty or holds a line break")
    return f"{CONTEXT}\n{name}\n{version}\n{sha256}\n".encode()


def sig_text(public: bytes, signature: bytes) -> str:
    """The text of a `.sig` file."""
    return f"alg: {ALG}\nkey: {key_id(public)}\nsig: {base64.b64encode(signature).decode('ascii')}\n"


def parse_sig(data: bytes) -> tuple[str, bytes]:
    """(key id, the 64 signature bytes) of a `.sig` file's content; `ValueError` for anything else."""
    try:
        m = _SIG_RE.fullmatch(data.decode("ascii"))
        if m is None:
            raise ValueError("not the three lines alg, key and sig")
        return m.group(1), base64.b64decode(m.group(2), validate=True)
    except (UnicodeDecodeError, binascii.Error) as e:
        raise ValueError(str(e)) from e


def sig_path(zip_path: Path) -> Path:
    """`<zip>.sig`, beside the zip."""
    return zip_path.with_name(zip_path.name + SIG_SUFFIX)


def sha256_file(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def package_version(zip_path: Path) -> tuple[str, str]:
    """(the `VERSION` in the zip, the member prefix it was found under): at the root, or inside the one
    top-level folder. `ValueError` with the sentence an update shows (`NS-E701`) for anything else."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            prefix = ""
            if "VERSION" not in names:
                tops = {n.split("/", 1)[0] for n in names if "/" in n}
                if len(tops) != 1 or f"{next(iter(tops))}/VERSION" not in names:
                    raise ValueError("the zip has no VERSION file at its root")
                prefix = next(iter(tops)) + "/"
            version = z.read(prefix + "VERSION").decode("utf-8").strip()
    except zipfile.BadZipFile as e:
        raise ValueError(f"{zip_path.name} is not a valid zip: {e}") from e
    if not version:
        raise ValueError("VERSION is empty")
    return version, prefix


class BadSignature(Exception):
    """Why a package is not one a trusted key signed. `kind`: `missing`, `unreadable`, `format`,
    `unknown_key` or `mismatch`; `key` is the key id the `.sig` names (when it could be read)."""

    def __init__(self, kind: str, detail: str = "", key: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail, self.key = kind, detail, key


def check_package(zip_path: Path, version: str, sha256: str, keys: Mapping[str, str]) -> str:
    """The id of the key in `keys` (`{key id: base64 public key}`) that signed the package; `BadSignature`
    otherwise. Reads `<zip>.sig` and nothing else: `version` and `sha256` are the zip's, as the caller has
    just read and verified them."""
    path = sig_path(zip_path)
    try:
        with path.open("rb") as fh:
            data = fh.read(SIG_MAX_BYTES + 1)
    except FileNotFoundError as e:
        raise BadSignature("missing") from e
    except OSError as e:
        raise BadSignature("unreadable", str(e)) from e
    try:
        if len(data) > SIG_MAX_BYTES:
            raise ValueError(f"longer than {SIG_MAX_BYTES} bytes")
        key, signature = parse_sig(data)
    except ValueError as e:
        raise BadSignature("format", str(e)) from e
    if key not in keys:
        raise BadSignature("unknown_key", key=key)
    try:
        public = base64.b64decode(keys[key], validate=True)
        signed = message(zip_path.name, version, sha256)
    except ValueError as e:      # (binascii.Error is one: a damaged key list verifies nothing)
        raise BadSignature("mismatch", str(e), key) from e
    if key_id(public) != key or not verify(public, signed, signature):
        raise BadSignature("mismatch", key=key)
    return key
