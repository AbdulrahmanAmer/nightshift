"""Windows integration through PowerShell (component C1): user environment variable, desktop, shortcuts.

This is the only module that names the home environment variable literally. Scripts are passed with
``-EncodedCommand`` so no quoting can go wrong.
"""

from __future__ import annotations

import base64
from pathlib import Path

from installer.stage2.runner import Runner, RunResult

HOME_VAR = "NIGHTSHIFT_HOME"


def ps_quote(value: str) -> str:
    """Escape a value for a single-quoted PowerShell string."""
    return value.replace("'", "''")


def run_ps(
    runner: Runner, script: str, *, tag: str, meta: dict[str, str] | None = None, timeout: float = 60
) -> RunResult:
    """Run a PowerShell snippet (no profile, no prompts)."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    return runner.run(argv, tag=tag, meta=meta, timeout=timeout)


def set_user_env(runner: Runner, name: str, value: str) -> bool:
    """Set a user-level environment variable. True on success."""
    script = f"[Environment]::SetEnvironmentVariable('{ps_quote(name)}', '{ps_quote(value)}', 'User')"
    return run_ps(runner, script, tag="ps:set-env", meta={"name": name, "value": value}).ok


def get_user_env(runner: Runner, name: str) -> str | None:
    """Read a user-level environment variable (None when unset)."""
    script = f"[Environment]::GetEnvironmentVariable('{ps_quote(name)}', 'User')"
    res = run_ps(runner, script, tag="ps:get-env", meta={"name": name})
    value = res.stdout.strip()
    return value or None


def desktop_dir(runner: Runner) -> Path | None:
    """The current user's Desktop folder (follows OneDrive redirection)."""
    res = run_ps(runner, "[Environment]::GetFolderPath('Desktop')", tag="ps:desktop")
    text = res.stdout.strip()
    return Path(text) if res.ok and text else None


def create_shortcut(
    runner: Runner, lnk: Path, target: str, arguments: str, workdir: Path, description: str
) -> bool:
    """Create a .lnk with WScript.Shell. True when PowerShell reported success."""
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{ps_quote(str(lnk))}'); "
        f"$s.TargetPath = '{ps_quote(target)}'; "
        f"$s.Arguments = '{ps_quote(arguments)}'; "
        f"$s.WorkingDirectory = '{ps_quote(str(workdir))}'; "
        f"$s.Description = '{ps_quote(description)}'; "
        "$s.Save()"
    )
    meta = {"lnk": str(lnk), "target": target, "arguments": arguments}
    return run_ps(runner, script, tag="ps:shortcut", meta=meta).ok
