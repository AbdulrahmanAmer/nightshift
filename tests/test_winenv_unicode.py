"""C1: user and Desktop paths with non-ASCII characters survive the PowerShell round trip.

Windows PowerShell 5.1 writes redirected stdout in the OEM code page, so the old plain-text read of the Desktop
path turned `C:\Users\José\Desktop` into U+FFFD and made shortcut creation (NS-E110) fail. These tests run the
real `powershell.exe` on a non-ASCII temp folder.
"""

import shutil
import sys
from pathlib import Path

import pytest

from installer.stage2 import winenv
from installer.stage2.runner import Runner
from installer.stage2.simulate import ps_b64

PS = shutil.which("powershell")
real_ps = pytest.mark.skipif(sys.platform != "win32" or PS is None, reason="needs Windows PowerShell")

NON_ASCII = "José Ñandú العربية"


def test_decode_round_trip_and_garbage() -> None:
    assert winenv.decode_utf8_b64(ps_b64(NON_ASCII) + "\r\n") == NON_ASCII
    assert winenv.decode_utf8_b64("") == "" and winenv.decode_utf8_b64("not base64 !!") == ""


@real_ps
def test_a_non_ascii_value_comes_back_exactly_from_real_powershell() -> None:
    value = winenv.eval_ps_text(Runner(), f"'{winenv.ps_quote(NON_ASCII)}'", tag="ps:test")
    assert value == NON_ASCII


@real_ps
def test_user_env_and_shortcut_in_a_non_ascii_folder(tmp_path: Path) -> None:
    desktop = tmp_path / NON_ASCII / "Desktop"
    desktop.mkdir(parents=True)
    runner = Runner()
    lnk = desktop / "Nightshift Dashboard.lnk"
    assert winenv.create_shortcut(runner, lnk, "powershell.exe", "-NoProfile", desktop, "Nightshift")
    assert lnk.is_file()
    # the path read back through PowerShell is the same string (what s10 does to find the diagnostics zip)
    back = winenv.eval_ps_text(runner, f"(Get-Item -LiteralPath '{winenv.ps_quote(str(lnk))}').FullName",
                               tag="ps:test")
    assert back == str(lnk)
