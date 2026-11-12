"""The one size formatter: every size the owner sees, in the units Windows shows.

Binary steps of 1024, labelled B, KB, MB and GB, as Explorer and the disk properties show file and disk
sizes (184.30 GB is what Windows shows for 197,895,757,824 bytes). Speeds are MB/s in the same steps. Setup,
the GPU kit and the Dashboard server all call this module, so no screen mixes decimal and binary units
(owner's ruling, findings G-17).
"""

from __future__ import annotations

KIB = 1024
MIB = 1024**2
GIB = 1024**3


def size_text(n: float | None) -> str:
    """``9.0 GB``, ``12.4 MB``, ``7.7 KB``, ``512 B``; ``?`` when the size is not known.

    A size under 1 MB is shown in KB (a 7,877-byte file is ``7.7 KB``, never ``0.0 MB``).
    """
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"  # unreachable; keeps the type checker content


def pair_text(done: float, total: float | None) -> str:
    """``8.4/20.9 GB``: two sizes in the one unit of the total (of ``done`` when the total is not known)."""
    ref = float(total) if total else float(done)
    unit, div = ("B", 1)
    for u, d in (("GB", GIB), ("MB", MIB), ("KB", KIB)):
        if ref >= d:
            unit, div = u, d
            break
    if unit == "B":
        return f"{done:.0f}/{total:.0f} B" if total else f"{done:.0f} B"
    if total:
        return f"{done / div:.1f}/{total / div:.1f} {unit}"
    return f"{done / div:.1f}/? {unit}"


def speed_text(bytes_per_s: float | None) -> str:
    """``11.4 MB/s``; under 0.1 MB/s in KB/s; ``?`` when it is not known."""
    if bytes_per_s is None:
        return "?"
    if abs(bytes_per_s) < MIB / 10:
        return f"{bytes_per_s / KIB:.0f} KB/s"
    return f"{bytes_per_s / MIB:.1f} MB/s"


def gb_value(n: float) -> float:
    """Bytes as a number of GB (binary), one decimal: the value of a ``free_gb`` field."""
    return round(n / GIB, 1)
