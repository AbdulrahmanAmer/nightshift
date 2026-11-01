"""C2 Windows toast through PowerShell `Windows.UI.Notifications` (spec J10). No extra package.

Title and message travel in environment variables, never inside the script text, so they cannot
break out of the PowerShell source. Everything is best effort: unavailable means no-op.
"""

from __future__ import annotations

import subprocess

from ns_supervisor import pwsh

TOAST_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime]",
        "[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime]",
        "$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)",
        "$text = $tpl.GetElementsByTagName('text')",
        "[void]$text.Item(0).AppendChild($tpl.CreateTextNode($env:NS_TOAST_TITLE))",
        "[void]$text.Item(1).AppendChild($tpl.CreateTextNode($env:NS_TOAST_MESSAGE))",
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)",
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Nightshift').Show($toast)",
    ]
)


def toast(title: str, message: str) -> bool:
    """Show a toast; True if PowerShell accepted it. Never raises (no PowerShell, headless: no-op)."""
    try:
        r = pwsh.run_powershell(
            TOAST_SCRIPT, env_extra={"NS_TOAST_TITLE": title, "NS_TOAST_MESSAGE": message}, timeout_s=30
        )
    except (pwsh.PowerShellMissing, OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0
