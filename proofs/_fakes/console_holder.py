"""A stand-in for the Nightshift Code console in proofs: a process that does nothing until it is ended.

`launchers/nightshift-code.ps1` sends its own PowerShell's pid with `/acquire llama owner opencode`
(tests/test_launchers.py pins that body); closing the console with X ends that process without running any
`finally`. A proof acquires with THIS process's pid in the same body, then ends it by that pid, which is what
the engine sees when the console is closed. It never runs longer than `argv[1]` seconds (default 600), so a
proof that is stopped halfway leaves nothing behind for long; the runner also stops it by its recorded pid.

    python console_holder.py [<seconds>]
"""

from __future__ import annotations

import sys
import time


def main(argv: list[str]) -> int:
    limit = float(argv[1]) if len(argv) > 1 else 600.0
    end = time.monotonic() + max(1.0, min(limit, 3600.0))
    while time.monotonic() < end:
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
