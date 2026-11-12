"""Reading the NVIDIA driver and CUDA version out of nvidia-smi: one reader for setup, product and kit.

The driver number is read from the ``driver_version`` column of ``--query-gpu`` (a column with one meaning in
every driver). The table's banner is only the fallback: driver 621.01 prints ``KMD Version:`` where older
drivers print ``Driver Version:``, and ``CUDA UMD Version:`` where older ones print ``CUDA Version:``
(findings G-1 and G-30).
"""

from __future__ import annotations

import re

_DRIVER_WORDS = re.compile(r"(?:Driver|KMD)\s+Version:\s*([\d.]+)")
_CUDA_WORDS = re.compile(r"CUDA(?:\s+UMD)?\s+Version:\s*([\d.]+)")
_NUMBER = re.compile(r"^\d+(?:\.\d+)+$")


def driver_from_query(csv_line: str | None, column: int = 1) -> str | None:
    """The driver of the first line of a ``--query-gpu=name,driver_version,...`` CSV answer (``--format=csv,
    noheader``); None when the line has no such column or it is not a version number."""
    line = ((csv_line or "").strip().splitlines() or [""])[0]
    cells = [c.strip() for c in line.split(",")]
    if len(cells) > column and _NUMBER.match(cells[column]):
        return cells[column]
    return None


def driver_from_banner(table: str | None) -> str | None:
    """The driver number of the banner, in either wording (``Driver Version:`` or ``KMD Version:``)."""
    m = _DRIVER_WORDS.search(table or "")
    return m.group(1) if m else None


def cuda_from_banner(table: str | None) -> str | None:
    """The CUDA version of the banner, in either wording (``CUDA Version:`` or ``CUDA UMD Version:``)."""
    m = _CUDA_WORDS.search(table or "")
    return m.group(1) if m else None


def read_driver(query_line: str | None, table: str | None) -> str | None:
    """The driver: the query's column first, the banner when the query did not give one."""
    return driver_from_query(query_line) or driver_from_banner(table)
