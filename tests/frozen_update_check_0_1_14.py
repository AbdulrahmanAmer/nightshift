"""A frozen copy of the package check of Nightshift 0.1.14 (`ns_supervisor/update.py` at tag v0.1.14).

The updater of an installed 0.1.14 cannot be changed: it checks a package with these three functions, the zip
against the `.sha256` beside it, and knows no `.sig`. An update FROM 0.1.14 into a signed version must
therefore still pass them with the `.sig` lying beside the zip (card update-signing). The text is the tag's,
function for function; only the imports this slice does not use are left out. Do not edit it.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from ns_common.errors import NsError


def sha256_file(path: Path) -> str:
    """Hex SHA256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_sidecar(zip_path: Path) -> str | None:
    """The expected digest from `<zip>.sha256` (`<hex>  <name>` or just `<hex>`), or None."""
    side = zip_path.with_name(zip_path.name + ".sha256")
    try:
        token = side.read_text(encoding="utf-8").split()[0].lower()
    except (OSError, IndexError):
        return None
    return token if len(token) == 64 and all(c in "0123456789abcdef" for c in token) else None


def verify_package(zip_path: Path) -> tuple[str, str]:
    """Check sidecar digest and VERSION; returns (version, member prefix). Raises `NS-E701`."""
    if not zip_path.is_file():
        raise NsError("NS-E701", f"{zip_path} does not exist")
    want = read_sidecar(zip_path)
    if want is None:
        raise NsError("NS-E701", f"missing or unreadable {zip_path.name}.sha256")
    got = sha256_file(zip_path)
    if got != want:
        raise NsError("NS-E701", f"SHA256 mismatch for {zip_path.name}: expected {want[:12]}, got {got[:12]}")
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            prefix = ""
            if "VERSION" not in names:
                tops = {n.split("/", 1)[0] for n in names if "/" in n}
                if len(tops) == 1 and f"{next(iter(tops))}/VERSION" in names:
                    prefix = next(iter(tops)) + "/"
                else:
                    raise NsError("NS-E701", "the zip has no VERSION file at its root")
            version = z.read(prefix + "VERSION").decode("utf-8").strip()
    except zipfile.BadZipFile as e:
        raise NsError("NS-E701", f"{zip_path.name} is not a valid zip: {e}") from e
    if not version:
        raise NsError("NS-E701", "VERSION is empty")
    return version, prefix
